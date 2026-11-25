"""Narração do comentário. Duas vozes, nesta ordem:

  1. ElevenLabs (voz neural na nuvem, paga por caractere), se houver ELEVENLABS_API_KEY no ambiente ou no `.env`;
  2. Piper (voz neural offline e gratuita). O programa (GitHub) e a voz (HuggingFace) são baixados para `voz/`
     na primeira vez que uma narração é pedida.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg, gasto, progresso
from .config import config

RAIZ = Path(__file__).resolve().parent.parent
PASTA = RAIZ / "voz"
PIPER_ZIP = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
# versão fixa: as vozes mais novas do repositório usam um formato que este programa não lê
VOZES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
PIPER_EN = "en/en_US/ryan/high/en_US-ryan-high"  # as vozes locais do catálogo são em português; em inglês é esta
IDIOMAS = {"pt": "português do Brasil", "en": "inglês"}
FRASE_AMOSTRA = ("Final de campeonato, um contra quatro, e ele decide ir pra cima sozinho. "
                 "E você, teria essa coragem? Comenta aí o que achou.")

ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/"
ELEVEN_MODELO = "eleven_multilingual_v2"  # troque com ELEVENLABS_MODEL no `.env`
# menos estabilidade e mais estilo deixam a fala mais viva; a velocidade fica um pouco acima do normal
ELEVEN_AJUSTES = {"stability": 0.4, "similarity_boost": 0.75, "style": 0.35, "use_speaker_boost": True, "speed": 1.08}


class NarracaoErro(RuntimeError):
    pass


@dataclass
class Narrado:
    arquivo: Path
    duracao: float
    palavras: list[dict] | None = None  # [{i, f, t}] com o tempo real de cada palavra, quando a voz informa


@dataclass(frozen=True)
class Voz:
    nome: str
    motor: str  # "elevenlabs" ou "piper"
    ref: str  # id da voz na ElevenLabs, ou caminho no repositório de vozes do Piper
    jeito: str  # descrição curta, mostrada na interface
    ajuste: tuple[float, float, float] = (0.94, 0.667, 0.8)  # Piper: velocidade, variação de entonação e de ritmo


CATALOGO = {
    "liam": Voz("Liam", "elevenlabs", "TX3LPaxmHKxFdv7VOQHJ", "jovem e enérgica"),
    "will": Voz("Will", "elevenlabs", "bIHbv24MWmeRgasZH58o", "jovem e descontraída"),
    "brian": Voz("Brian", "elevenlabs", "nPczCjzI2devNBz1zQrb", "grave, de narrador"),
    "eric": Voz("Eric", "elevenlabs", "cjVigY5qzO86Huf0OWal", "adulta e suave"),
    "faber": Voz("Faber", "piper", "pt/pt_BR/faber/medium/pt_BR-faber-medium", "local, gratuita"),
    "cadu": Voz("Cadu", "piper", "pt/pt_BR/cadu/medium/pt_BR-cadu-medium", "local, gratuita", (0.88, 0.8, 0.9)),
    "jeff": Voz("Jeff", "piper", "pt/pt_BR/jeff/medium/pt_BR-jeff-medium", "local, gratuita", (0.88, 0.8, 0.9)),
    "edresson": Voz("Edresson", "piper", "pt/pt_BR/edresson/low/pt_BR-edresson-low", "local, gratuita", (0.88, 0.8, 0.9)),
}
RESERVA = "faber"  # voz local usada quando a ElevenLabs não está disponível


def vozes() -> dict[str, Voz]:
    """O catálogo, mais a voz da ElevenLabs que o dono do canal tiver posto em ELEVENLABS_VOICE_ID no `.env`."""
    propria = config("ELEVENLABS_VOICE_ID")
    extra = {"minha": Voz("Minha voz", "elevenlabs", propria, "definida no .env")} if propria else {}
    return extra | CATALOGO


def tem_eleven() -> bool:
    return bool(config("ELEVENLABS_API_KEY"))


def padrao() -> str:
    """A voz usada quando nenhuma é escolhida."""
    if not tem_eleven():
        return RESERVA
    return "minha" if config("ELEVENLABS_VOICE_ID") else "liam"


_eleven_falhou = False  # depois da primeira falha, o resto da rodada usa a voz local sem repetir o erro


def falar(texto: str, idioma: str, destino: Path, voz: str = "") -> Narrado:
    """Grava `texto` falado com a voz pedida (ou a padrão). `destino` vem sem extensão: cada voz tem o seu formato."""
    global _eleven_falhou
    limpo = " ".join(texto.split())
    escolhida = vozes().get(voz) or vozes()[padrao()]
    if escolhida.motor == "elevenlabs":
        if tem_eleven() and not _eleven_falhou:
            try:
                return _eleven(limpo, destino.with_suffix(".mp3"), escolhida.ref)
            except NarracaoErro as erro:
                _eleven_falhou = True
                progresso.aviso(f"ElevenLabs indisponível ({erro}). Usando a voz local nesta rodada.")
        elif not _eleven_falhou:
            _eleven_falhou = True
            progresso.aviso("A voz escolhida é da ElevenLabs, mas não há ELEVENLABS_API_KEY. Usando a voz local.")
        escolhida = CATALOGO[RESERVA]
    return _piper(limpo, idioma, destino.with_suffix(".wav"), escolhida)


def amostra(voz: str) -> Path:
    """Arquivo com a frase de exemplo falada pela voz, para ouvir antes de escolher. Gravado uma vez e guardado."""
    escolhida = vozes().get(voz)
    if escolhida is None:
        raise NarracaoErro("voz desconhecida")
    pasta = PASTA / "amostras"
    mp3 = pasta / f"{voz}.mp3"
    if voz == "minha":  # o id pode mudar no `.env`: a amostra guardada vale só para o id com que foi gravada
        mp3 = pasta / f"minha-{escolhida.ref[:12]}.mp3"
    if mp3.exists():
        return mp3
    pasta.mkdir(parents=True, exist_ok=True)
    if escolhida.motor == "elevenlabs":
        if not tem_eleven():
            raise NarracaoErro("esta voz precisa da ELEVENLABS_API_KEY")
        _eleven(FRASE_AMOSTRA, mp3, escolhida.ref)
    else:
        wav = pasta / f"{voz}.wav"
        _piper(FRASE_AMOSTRA, "pt", wav, escolhida)
        try:
            ffmpeg.rodar(["-i", str(wav), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100", "-b:a", "128k", str(mp3)])
        finally:
            wav.unlink(missing_ok=True)
    return mp3


def _eleven(texto: str, destino: Path, voz: str) -> Narrado:
    corpo = {"text": texto, "model_id": config("ELEVENLABS_MODEL", ELEVEN_MODELO), "voice_settings": ELEVEN_AJUSTES}
    pedido = urllib.request.Request(
        f"{ELEVEN_URL}{voz}/with-timestamps?output_format=mp3_44100_128", data=json.dumps(corpo).encode("utf-8"),
        headers={"xi-api-key": config("ELEVENLABS_API_KEY"), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(pedido, timeout=90) as resposta:
            dados = json.loads(resposta.read())
    except urllib.error.HTTPError as erro:
        try:
            detalhe = json.loads(erro.read()).get("detail", {})
            motivo = detalhe.get("message") or detalhe.get("status") or str(detalhe) if isinstance(detalhe, dict) else str(detalhe)
        except (ValueError, AttributeError):
            motivo = ""
        raise NarracaoErro(f"erro {erro.code}: {motivo[:200]}" if motivo else f"erro {erro.code}") from erro
    except (OSError, ValueError) as erro:
        raise NarracaoErro(f"sem resposta ({erro})") from erro
    try:
        destino.write_bytes(base64.b64decode(dados["audio_base64"]))
    except (KeyError, TypeError, ValueError) as erro:
        raise NarracaoErro("resposta sem áudio") from erro
    gasto.voz(len(texto))  # a ElevenLabs cobra por caractere
    return Narrado(destino, ffmpeg.duracao(destino), _palavras(dados.get("alignment")))


def _palavras(alinhamento: dict | None) -> list[dict] | None:
    """Junta os tempos por caractere que a ElevenLabs devolve em tempos por palavra."""
    try:
        letras = alinhamento["characters"]
        inicios, fins = alinhamento["character_start_times_seconds"], alinhamento["character_end_times_seconds"]
    except (KeyError, TypeError):
        return None
    palavras: list[dict] = []
    atual = None
    for letra, ini, fim in zip(letras, inicios, fins):
        if letra.isspace():
            atual = None
        elif atual is None:
            atual = {"i": float(ini), "f": float(fim), "t": letra}
            palavras.append(atual)
        else:
            atual["t"] += letra
            atual["f"] = float(fim)
    return palavras or None


def _baixar(url: str, destino: Path) -> None:
    tmp = destino.with_name(destino.name + ".baixando")
    try:
        pedido = urllib.request.Request(url, headers={"User-Agent": "corte-maker"})
        with urllib.request.urlopen(pedido, timeout=60) as resposta, tmp.open("wb") as arquivo:
            shutil.copyfileobj(resposta, arquivo, 1 << 20)
    except OSError as erro:
        tmp.unlink(missing_ok=True)
        raise NarracaoErro(f"não foi possível baixar {url.rsplit('/', 1)[-1]} ({erro})") from erro
    tmp.replace(destino)


def _programa() -> Path:
    exe = PASTA / "piper" / "piper.exe"
    if exe.exists():
        return exe
    if sys.platform != "win32":
        raise NarracaoErro("a narração automática só está preparada para Windows")
    PASTA.mkdir(exist_ok=True)
    progresso.info("Baixando o programa de voz (~22 MB, só na primeira vez)...")
    pacote = PASTA / "piper.zip"
    _baixar(PIPER_ZIP, pacote)
    try:
        with zipfile.ZipFile(pacote) as z:
            raiz = PASTA.resolve()
            if any(not (raiz / nome).resolve().is_relative_to(raiz) for nome in z.namelist()):
                raise NarracaoErro("o pacote do programa de voz tem caminhos inesperados")
            z.extractall(PASTA)
    except zipfile.BadZipFile as erro:
        raise NarracaoErro("o pacote do programa de voz veio corrompido") from erro
    finally:
        pacote.unlink(missing_ok=True)
    if not exe.exists():
        raise NarracaoErro("o pacote do programa de voz não trouxe o piper.exe")
    return exe


def _modelo(caminho: str) -> Path:
    modelo = PASTA / (caminho.rsplit("/", 1)[-1] + ".onnx")
    cfg = modelo.with_name(modelo.name + ".json")
    if not (modelo.exists() and cfg.exists()):
        PASTA.mkdir(exist_ok=True)
        progresso.info("Baixando a voz local (60 a 120 MB, só na primeira vez)...")
        _baixar(f"{VOZES_URL}{caminho}.onnx.json", cfg)
        _baixar(f"{VOZES_URL}{caminho}.onnx", modelo)
    return modelo


def _piper(texto: str, idioma: str, destino: Path, voz: Voz) -> Narrado:
    exe, modelo = _programa(), _modelo(PIPER_EN if idioma == "en" else voz.ref)
    velocidade, entonacao, ritmo = voz.ajuste
    try:
        r = subprocess.run([str(exe), "--model", str(modelo), "--output_file", str(destino.resolve()),
                            "--length_scale", str(velocidade), "--noise_scale", str(entonacao),
                            "--noise_w", str(ritmo), "--sentence_silence", "0.12"],
                           input=texto.encode("utf-8"), capture_output=True, cwd=exe.parent, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as erro:
        raise NarracaoErro(f"o programa de voz não rodou ({erro})") from erro
    if r.returncode != 0 or not destino.exists():
        raise NarracaoErro(f"o programa de voz falhou: {r.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return Narrado(destino, ffmpeg.duracao(destino))
