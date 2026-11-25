"""Transcrição com tempo por palavra (faster-whisper, na GPU quando disponível)."""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import sys
import threading
import wave
from pathlib import Path

import numpy as np

from . import gasto, progresso

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
_uma_por_vez = threading.Lock()


@functools.cache
def _preparar_cuda() -> None:
    """No Windows as DLLs do CUDA vêm de pacotes pip e precisam estar no PATH antes de carregar o modelo.

    Uma vez só por processo. Feito a cada transcrição, o PATH ganhava as mesmas pastas de novo (~190 caracteres por
    vez) e, depois de umas 170 transcrições, passava do limite do Windows (32.767): daí em diante nenhum programa
    abria mais (ffmpeg, Claude), com "[WinError 8] Não há recursos de memória...", até fechar o corte-maker."""
    if sys.platform != "win32":
        return
    spec = importlib.util.find_spec("nvidia")
    if not spec or not spec.submodule_search_locations:
        return
    no_path = os.environ.get("PATH", "").split(os.pathsep)
    for raiz in spec.submodule_search_locations:
        for pasta in Path(raiz).glob("*/bin"):
            os.add_dll_directory(str(pasta))
            if str(pasta) not in no_path:
                no_path.insert(0, str(pasta))
    os.environ["PATH"] = os.pathsep.join(no_path)


def modelo_baixado(modelo: str) -> bool:
    from faster_whisper.utils import download_model

    try:
        download_model(modelo, local_files_only=True)
        return True
    except Exception:
        return False


@functools.cache
def _carregar(modelo: str, dispositivo: str):
    """O modelo fica na memória entre chamadas: no modo ao vivo ele é usado a cada poucos minutos."""
    from faster_whisper import WhisperModel

    return WhisperModel(modelo, device=dispositivo, compute_type="float16" if dispositivo == "cuda" else "int8")


def _rodar(wav: Path, modelo: str, idioma: str | None, duracao: float, dispositivo: str) -> dict:
    from faster_whisper import BatchedInferencePipeline

    # o wav já está em 16 kHz mono; entregar as amostras direto evita o decodificador interno do faster-whisper
    with wave.open(str(wav), "rb") as w:
        amostras = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    gasto.transcricao(len(amostras) / 16000)  # hoje sai de graça na placa local; numa API seria cobrado por minuto

    segmentos, meta = BatchedInferencePipeline(_carregar(modelo, dispositivo)).transcribe(
        amostras, language=idioma, word_timestamps=True, vad_filter=True,
        batch_size=16 if dispositivo == "cuda" else 4,
    )
    palavras = []
    proximo_aviso = 10
    for seg in segmentos:
        for p in seg.words or []:
            texto = p.word.strip()
            if texto:
                palavras.append({"i": round(float(p.start), 2), "f": round(float(p.end), 2), "t": texto})
        if duracao > 0:
            pct = 100 * seg.end / duracao
            progresso.pct(pct)
            if pct >= proximo_aviso:
                progresso.info(f"{min(int(pct), 100)}% transcrito")
                proximo_aviso = (int(pct) // 10 + 1) * 10
    palavras.sort(key=lambda p: p["i"])
    return {"idioma": meta.language, "palavras": palavras}


def transcrever(wav: Path, cache: Path | None, modelo: str, idioma: str | None, duracao: float) -> dict:
    """Devolve {"idioma": "pt", "palavras": [{"i": inicio, "f": fim, "t": texto}, ...]}. `cache=None` não guarda."""
    if cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    with _uma_por_vez:  # várias lives compartilham o mesmo modelo na placa de vídeo: uma transcrição de cada vez
        _preparar_cuda()
        try:
            resultado = _rodar(wav, modelo, idioma, duracao, "cuda")
        except Exception as erro:  # sem GPU/driver/DLL: cai para CPU em vez de abortar
            progresso.aviso(f"GPU indisponível para transcrição ({type(erro).__name__}: {erro}). Usando CPU, mais lento.")
            resultado = _rodar(wav, modelo, idioma, duracao, "cpu")

    if cache:
        cache.write_text(json.dumps(resultado, ensure_ascii=False), encoding="utf-8")
    return resultado
