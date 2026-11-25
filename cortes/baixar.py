"""Obtém o material de origem.

Para links, o caminho normal é econômico: baixa só o áudio (para transcrever e analisar) e, depois de escolhidos
os momentos, só os trechos de vídeo que viram corte. Baixar o vídeo inteiro fica como alternativa.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg, progresso


@dataclass
class Fonte:
    titulo: str
    canal: str
    url: str  # vazio para arquivo local
    video: Path  # mídia usada na análise: o vídeo inteiro ou, no caminho econômico, só o áudio
    pasta: Path  # pasta de trabalho (cache de transcrição e de momentos)


def _slug(texto: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", texto).strip("_")[:60] or "video"


def eh_link(origem: str) -> bool:
    return re.match(r"^https?://", origem.strip(), re.I) is not None


def local(origem: str, trabalho: Path) -> Fonte:
    video = Path(origem).expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(f"Arquivo não encontrado: {video}")
    st = video.stat()
    marca = hashlib.sha1(f"{video}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:8]
    pasta = trabalho / f"local_{_slug(video.stem)}_{marca}"
    pasta.mkdir(parents=True, exist_ok=True)
    return Fonte(video.stem, "", "", video, pasta)


def _ao_baixar(d: dict) -> None:
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    if d.get("status") == "downloading" and total:
        progresso.pct(100 * d.get("downloaded_bytes", 0) / total)


def _ydl(**extra):
    import yt_dlp
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

    # o download de trechos consulta esta variável global, não a opção `ffmpeg_location`
    FFmpegPostProcessor._ffmpeg_location.set(ffmpeg.exe())
    return yt_dlp.YoutubeDL({
        "ffmpeg_location": ffmpeg.exe(),
        "js_runtimes": {"deno": {}, "node": {}},  # o YouTube exige um runtime JS
        "noplaylist": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": 4,
        **extra,
    })


def _faixa(inicio: float | None, fim: float | None):
    from yt_dlp.utils import download_range_func

    return download_range_func(None, [(inicio or 0, fim if fim is not None else float("inf"))])


def _sufixo(inicio: float | None, fim: float | None) -> str:
    if inicio is None and fim is None:
        return ""
    return f"_{int(inicio or 0)}-{int(fim) if fim is not None else 'fim'}"


def _arquivo(dados: dict) -> Path:
    baixados = dados.get("requested_downloads") or []
    if not baixados:
        raise RuntimeError("O download não gerou nenhum arquivo.")
    return Path(baixados[0]["filepath"])


def _fonte(dados: dict, url: str, arquivo: Path) -> Fonte:
    return Fonte(
        titulo=dados.get("title") or arquivo.stem,
        canal=dados.get("channel") or dados.get("uploader") or "",
        url=dados.get("webpage_url") or url,
        video=arquivo,
        pasta=arquivo.parent,
    )


def consultar(url: str) -> dict:
    """Dados do vídeo (título, canal, formatos, se está ao vivo), sem baixar nada."""
    with _ydl() as ydl:
        return ydl.extract_info(url.strip(), download=False)


def audio(dados: dict, url: str, trabalho: Path, inicio: float | None, fim: float | None) -> Fonte:
    """Baixa só a faixa de áudio (dezenas de MB por hora, em vez de GB)."""
    sufixo = _sufixo(inicio, fim)
    extra = {"download_ranges": _faixa(inicio, fim)} if sufixo else {}
    with _ydl(outtmpl=str(trabalho / f"%(extractor)s_%(id)s{sufixo}" / "audio.%(ext)s"),
              format="ba/w",  # só áudio; se o site não separar, a menor versão com vídeo
              progress_hooks=[_ao_baixar], **extra) as ydl:
        feito = ydl.process_ie_result(copy.deepcopy(dados), download=True)
    return _fonte(feito, url, _arquivo(feito))


def trecho(dados: dict, inicio: float, fim: float, base: Path) -> Path:
    """Baixa só o intervalo [inicio, fim] do vídeo, em até 1080p. O corte cai no quadro-chave mais próximo,
    então o começo real pode variar alguns segundos: quem chama alinha pelo áudio."""
    with _ydl(outtmpl=f"{base}.%(ext)s", format="bv*+ba/b", format_sort=["res:1080", "vcodec:h264", "acodec:aac"],
              merge_output_format="mp4", overwrites=True, download_ranges=_faixa(max(0.0, inicio), fim)) as ydl:
        return _arquivo(ydl.process_ie_result(copy.deepcopy(dados), download=True))


def inteiro(dados: dict, url: str, trabalho: Path, inicio: float | None, fim: float | None) -> Fonte:
    """Alternativa: baixa o vídeo todo (ou a faixa pedida) em até 1080p."""
    sufixo = _sufixo(inicio, fim)
    extra = {"download_ranges": _faixa(inicio, fim)} if sufixo else {}
    with _ydl(outtmpl=str(trabalho / f"%(extractor)s_%(id)s{sufixo}" / "video.%(ext)s"),
              # prefere H.264 + AAC: decodifica rápido e junta em mp4 sem recodificar
              format="bv*+ba/b", format_sort=["res:1080", "vcodec:h264", "acodec:aac"], merge_output_format="mp4",
              progress_hooks=[_ao_baixar], **extra) as ydl:
        feito = ydl.process_ie_result(copy.deepcopy(dados), download=True)
    return _fonte(feito, url, _arquivo(feito))
