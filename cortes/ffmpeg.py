"""Acesso ao ffmpeg embutido (imageio-ffmpeg) e leitura de metadados do vídeo."""

from __future__ import annotations

import functools
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path


@functools.cache
def exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


class Cancelado(RuntimeError):
    pass


# Quem quer poder interromper o ffmpeg no meio (refazer um corte) põe aqui um threading.Event, na própria thread:
# `rodar` confere o evento enquanto espera e mata o ffmpeg quando ele é ligado.
cancelavel = threading.local()


def rodar(args: list[str], cwd: Path | None = None) -> None:
    cmd = [exe(), "-hide_banner", "-loglevel", "error", "-y", *args]
    parar = getattr(cancelavel, "evento", None)
    if parar is None:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        codigo, erro = r.returncode, r.stderr
    else:
        if parar.is_set():
            raise Cancelado("cancelado")
        with subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                              encoding="utf-8", errors="replace") as p:
            leitor = threading.Thread(target=lambda: setattr(p, "_erro", p.stderr.read()), daemon=True)
            leitor.start()  # lê o stderr enquanto roda, para o ffmpeg não travar com o cano cheio
            while p.poll() is None:
                if parar.wait(0.2):
                    p.kill()
                    p.wait()
                    raise Cancelado("cancelado")
            leitor.join(5)
            codigo, erro = p.returncode, getattr(p, "_erro", "") or ""
    if codigo != 0:
        raise RuntimeError(f"ffmpeg falhou:\n{erro.strip()[-2000:]}")


def saida(args: list[str]) -> bytes:
    """Roda o ffmpeg e devolve o que ele escreveu na saída padrão (ex.: áudio cru com `-f s16le -`)."""
    r = subprocess.run([exe(), "-hide_banner", "-loglevel", "error", *args], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou:\n{r.stderr.decode('utf-8', 'replace').strip()[-2000:]}")
    return r.stdout


def duracao(midia: Path) -> float:
    """Duração em segundos de qualquer arquivo de mídia, inclusive só de áudio."""
    import av

    with av.open(str(midia)) as c:
        if c.duration:
            return float(c.duration / av.time_base)
        s = c.streams[0]
        return float(s.duration * s.time_base) if s.duration else 0.0


@functools.cache
def tem_nvenc() -> bool:
    """Testa de verdade o encoder da placa NVIDIA (existir no build não garante que funcione)."""
    cmd = [exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
           "-c:v", "h264_nvenc", "-f", "null", "-"]
    return subprocess.run(cmd, capture_output=True).returncode == 0


@dataclass
class Info:
    duracao: float
    largura: int
    altura: int
    fps: float
    tem_audio: bool


def info(video: Path) -> Info:
    import av

    with av.open(str(video)) as c:
        if not c.streams.video:
            raise RuntimeError(f"{video.name} não tem faixa de vídeo")
        v = c.streams.video[0]
        duracao = float(c.duration / av.time_base) if c.duration else float(v.duration * v.time_base)
        fps = float(v.average_rate) if v.average_rate else 30.0
        return Info(duracao, v.codec_context.width, v.codec_context.height, fps, bool(c.streams.audio))
