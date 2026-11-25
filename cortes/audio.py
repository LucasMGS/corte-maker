"""Extração do áudio e curva de "empolgação" (volume em relação ao normal do vídeo)."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from . import ffmpeg

PASSO = 0.5  # segundos por amostra da curva de energia
TAXA = 16000


def extrair_wav(video: Path, wav: Path) -> None:
    if wav.exists():
        return
    tmp = wav.with_suffix(".tmp.wav")
    ffmpeg.rodar(["-i", str(video), "-vn", "-ac", "1", "-ar", str(TAXA), "-c:a", "pcm_s16le", str(tmp)])
    tmp.replace(wav)


def _envelope(amostras: np.ndarray, quadro: int) -> np.ndarray:
    n = len(amostras) // quadro
    x = amostras[: n * quadro].astype(np.float32).reshape(n, quadro)
    return np.log1p(np.sqrt((x ** 2).mean(axis=1)))


def alinhar(wav: Path, trecho: Path, esperado: float, busca: float = 10.0) -> float:
    """Em que segundo de `wav` começa o arquivo `trecho` (um pedaço do mesmo vídeo baixado à parte).

    Trechos baixados começam no quadro-chave mais próximo, não no segundo pedido. Aqui o desenho do volume do
    trecho é procurado no áudio de referência em volta do ponto esperado, com precisão de 10 ms. Se o casamento
    for fraco (silêncio, por exemplo), devolve o ponto esperado.
    """
    por_seg = 100
    quadro = TAXA // por_seg
    cru = ffmpeg.saida(["-i", str(trecho), "-vn", "-ac", "1", "-ar", str(TAXA), "-t", "45", "-f", "s16le", "-"])
    a = _envelope(np.frombuffer(cru, dtype=np.int16), quadro)
    inicio_ref = max(0.0, esperado - busca)
    with wave.open(str(wav), "rb") as w:
        w.setpos(min(int(inicio_ref * TAXA), w.getnframes()))
        b = _envelope(np.frombuffer(w.readframes(int((2 * busca + 45) * TAXA)), dtype=np.int16), quadro)
    a, b = a[: len(b) - 1].astype(np.float64), b.astype(np.float64)
    L = len(a)
    if L < 3 * por_seg:
        return esperado
    a = a - a.mean()
    # correlação normalizada de `a` com cada janela de `b` do mesmo tamanho
    produto = np.correlate(b, a, mode="valid")
    soma = np.cumsum(np.concatenate([[0.0], b]))
    soma2 = np.cumsum(np.concatenate([[0.0], b ** 2]))
    variancia = (soma2[L:] - soma2[:-L]) - (soma[L:] - soma[:-L]) ** 2 / L
    coef = produto / np.sqrt(np.maximum(variancia, 1e-9) * float((a * a).sum()) + 1e-12)
    k = int(coef.argmax())
    return inicio_ref + k / por_seg if coef[k] >= 0.5 else esperado


def energia(wav: Path) -> np.ndarray:
    """Volume a cada PASSO segundos, em desvios acima/abaixo do nível típico do vídeo (z robusto)."""
    return normalizar(volume(wav))


def normalizar(db: np.ndarray, referencia: np.ndarray | None = None) -> np.ndarray:
    """Converte dB em z robusto. `referencia` é de onde sai o "nível típico" (padrão: o próprio trecho);
    o modo ao vivo passa o histórico da live, para a régua não mudar a cada janela analisada."""
    ref = db if referencia is None or len(referencia) == 0 else referencia
    if len(ref) == 0:
        return np.zeros(0, dtype=np.float32)
    mediana = np.median(ref)
    mad = np.median(np.abs(ref - mediana)) * 1.4826
    return ((db - mediana) / (mad + 1e-6)).astype(np.float32)


def volume(wav: Path) -> np.ndarray:
    """Volume em dB a cada PASSO segundos."""
    bloco = int(TAXA * PASSO)
    db: list[np.ndarray] = []
    with wave.open(str(wav), "rb") as w:
        while True:
            dados = w.readframes(bloco * 240)  # 2 min por leitura
            if not dados:
                break
            x = np.frombuffer(dados, dtype=np.int16).astype(np.float32)
            n = len(x) // bloco
            if n == 0:
                break
            rms = np.sqrt((x[: n * bloco].reshape(n, bloco) ** 2).mean(axis=1))
            db.append(20 * np.log10(rms + 1.0))
    return np.concatenate(db) if db else np.zeros(0, dtype=np.float32)
