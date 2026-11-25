"""Renderiza um corte em 1080x1920 (fundo desfocado, fundo preto, tela cheia ou câmera em cima do jogo), com título,
legenda e áudio normalizado."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import ffmpeg, legendas
from .audio import TAXA

L, A = 1080, 1920
LAYOUTS = ("blur", "cheio", "inteiro", "preto", "camera")
ESCURO = 0.4  # quanto a imagem parada escurece enquanto o narrador fala

# Ritmo: silêncio de verdade dentro do corte é encurtado, e o das pontas sai quase todo.
SILENCIO_DB = 25.0  # quantos dB abaixo do volume típico do corte contam como silêncio
PAUSA_MIN = 1.0  # só mexe em silêncio que dure pelo menos isto, em segundos
RESPIRO = (0.25, 0.2)  # quanto de silêncio fica depois de um som e antes do seguinte, para a fala não atropelar
BORDAS = (0.1, 0.3)  # quanto de silêncio fica no começo e no fim do corte


@dataclass
class Fala:
    """Um comentário do narrador já gravado."""
    arquivo: Path
    duracao: float
    texto: str
    traducao: str = ""  # o mesmo comentário no outro idioma, para a segunda linha da legenda
    marcas: list[dict] | None = None  # tempo real de cada palavra ({i, f, t}), quando a voz informa

    def palavras(self, comeco: float) -> list[dict]:
        """Legenda da fala. Sem o tempo real de cada palavra, ele é estimado pelo tamanho dela."""
        if self.marcas:
            return [{"i": comeco + m["i"], "f": comeco + m["f"], "t": m["t"], "cor": legendas.COR_NARRADOR}
                    for m in self.marcas]
        partes = self.texto.split()
        pesos = [len(p) + 2 for p in partes]
        saida, t = [], comeco
        for parte, peso in zip(partes, pesos):
            d = self.duracao * peso / sum(pesos)
            saida.append({"i": t, "f": t + d, "t": parte, "cor": legendas.COR_NARRADOR})
            t += d
        return saida


@dataclass
class Geometria:
    filtro: str  # filtergraph que leva [0:v] até [v0]
    y_legenda: int
    titulo_pos: tuple[int, int, int]  # (alinhamento ASS, x, y)
    titulo_fixo: bool  # título o corte inteiro (fica sobre o fundo) ou só na abertura


def ler_camera(texto: str) -> tuple[float, float, float, float] | None:
    """Onde fica a câmera no vídeo original, em frações do quadro: "x,y,largura,altura" (0 a 1). Vazio: None."""
    if not texto.strip():
        return None
    try:
        x, y, w, h = (float(p) for p in texto.split(","))
    except ValueError:
        raise ValueError("a posição da câmera deve ser quatro números: x,y,largura,altura") from None
    if not (0 <= x < 1 and 0 <= y < 1 and 0.03 <= w <= 1 - x + 0.002 and 0.03 <= h <= 1 - y + 0.002):
        raise ValueError("a posição da câmera está fora do quadro")
    return x, y, min(w, 1 - x), min(h, 1 - y)


def geometria(info: ffmpeg.Info, layout: str, camera: tuple[float, float, float, float] | None = None) -> Geometria:
    aspecto = info.largura / info.altura
    if layout == "cheio" or aspecto <= 0.7 or (layout == "camera" and camera is None):
        # tela cheia: recorta o centro (ou o vídeo já é vertical)
        filtro = f"[0:v]scale={L}:{A}:force_original_aspect_ratio=increase,crop={L}:{A},setsar=1[v0]"
        return Geometria(filtro, 1380, (8, 540, 230), False)

    if layout == "camera":
        # câmera em cima, jogo embaixo. A altura da câmera segue a proporção dela (sem cortar muito), entre um
        # terço e metade da tela; o jogo preenche o resto, recortado no centro.
        cx, cy, cw, ch = camera
        px = [int(round(v)) for v in (cx * info.largura, cy * info.altura, cw * info.largura, ch * info.altura)]
        px[2], px[3] = max(2, px[2] // 2 * 2), max(2, px[3] // 2 * 2)
        alt_cam = int(round(min(max(L * px[3] / px[2], 640), 960) / 2) * 2)
        alt_jogo = A - alt_cam
        filtro = (
            "[0:v]split=2[c][j];"
            f"[c]crop={px[2]}:{px[3]}:{px[0]}:{px[1]},"
            f"scale={L}:{alt_cam}:force_original_aspect_ratio=increase:flags=lanczos,crop={L}:{alt_cam}[cam];"
            f"[j]scale={L}:{alt_jogo}:force_original_aspect_ratio=increase:flags=lanczos,crop={L}:{alt_jogo}[jogo];"
            "[cam][jogo]vstack=inputs=2,setsar=1[v0]"
        )
        # legenda logo abaixo da emenda, sobre o jogo; título só na abertura, no alto da câmera
        return Geometria(filtro, min(alt_cam + 150, 1500), (8, 540, 230), False)

    # vídeo inteiro sobre fundo preto: sem desfoque, nada cortado
    if layout == "preto":
        alt_frente = int(round(L / aspecto / 2) * 2)
        topo = (A - alt_frente) // 2
        filtro = (f"[0:v]scale={L}:{alt_frente}:flags=lanczos,"
                  f"pad={L}:{A}:0:{topo}:color=black,setsar=1[v0]")
        return Geometria(filtro, min(topo + alt_frente + 115, 1500), (2, 540, topo - 40), True)

    # vídeo sobre fundo desfocado: "blur" aproxima cortando as laterais até 4:3, "inteiro" mostra tudo
    alvo = min(aspecto, 4 / 3) if layout == "blur" else aspecto
    alt_frente = int(round(L / alvo / 2) * 2)
    topo = (A - alt_frente) // 2
    filtro = (
        "[0:v]split=2[f][t];"
        f"[f]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=10,"
        f"scale={L}:{A}:flags=bilinear,eq=brightness=-0.12[fundo];"
        f"[t]crop='min(iw,ih*{alvo:.5f})':ih,scale={L}:{alt_frente}:flags=lanczos[frente];"
        f"[fundo][frente]overlay=0:{topo},setsar=1[v0]"
    )
    y_legenda = min(topo + alt_frente + 115, 1500)  # abaixo do vídeo, sem entrar na área dos botões do app
    return Geometria(filtro, y_legenda, (2, 540, topo - 40), True)


def codec() -> list[str]:
    """Codificador de vídeo: o da placa NVIDIA quando existe, senão o x264."""
    if ffmpeg.tem_nvenc():
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "22", "-b:v", "0",
                "-maxrate", "10M", "-bufsize", "20M"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def capa(corte: Path, jpg: Path) -> None:
    """Miniatura do corte para a galeria da interface."""
    tmp = corte.parent / "_capa.jpg"  # nome fixo: `%` no título seria lido pelo ffmpeg como padrão de sequência
    ffmpeg.rodar(["-ss", "1", "-i", str(corte.resolve()), "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", tmp.name],
                 cwd=corte.parent)
    tmp.replace(jpg)


def silencios(video: Path, inicio: float, clipe: float) -> list[tuple[float, float]]:
    """Trechos de silêncio a tirar do corte, em segundos contados do início dele.

    Silêncio aqui é o som cair bem abaixo do volume típico do próprio corte (pausa com o microfone mudo, tela de
    carregamento sem som). Pausa na fala com o som do jogo por cima não entra: ali pode estar a ação.
    """
    cru = ffmpeg.saida(["-ss", f"{inicio:.3f}", "-t", f"{clipe:.3f}", "-i", str(video), "-vn", "-ac", "1",
                        "-ar", str(TAXA), "-f", "s16le", "-"])
    passo = 0.05
    quadro = int(TAXA * passo)
    x = np.frombuffer(cru, dtype=np.int16).astype(np.float32)
    n = len(x) // quadro
    if n * passo < 4:
        return []
    db = 20 * np.log10(np.sqrt((x[: n * quadro].reshape(n, quadro) ** 2).mean(axis=1)) + 1.0)
    quieto = db < np.percentile(db, 70) - SILENCIO_DB
    tirar: list[tuple[float, float]] = []
    i = 0
    while i < n:
        if not quieto[i]:
            i += 1
            continue
        j = i
        while j < n and quieto[j]:
            j += 1
        a, b = i * passo, j * passo
        if b - a >= PAUSA_MIN:
            if i == 0:
                tirar.append((0.0, b - BORDAS[0]))
            elif j == n:
                tirar.append((a + BORDAS[1], clipe))
            else:
                tirar.append((a + RESPIRO[0], b - RESPIRO[1]))
        i = j
    sobra = clipe - sum(b - a for a, b in tirar)
    # tirar quase metade do corte é sinal de áudio estranho (música muito dinâmica, por exemplo): melhor não mexer
    return tirar if sobra >= max(5.0, clipe * 0.6) else []


def sobra(tirar: list[tuple[float, float]], clipe: float) -> list[tuple[float, float]]:
    """O que sobra do corte depois de tirar os silêncios."""
    manter, t = [], 0.0
    for a, b in tirar:
        if a - t > 0.05:
            manter.append((t, a))
        t = b
    if clipe - t > 0.05:
        manter.append((t, clipe))
    return manter


def emendar(manter: list[tuple[float, float]], bruto: float) -> tuple[str, str, str]:
    """Filtergraph que tira de [0:v] e [0:a] os pedaços de `manter` (imagem e som juntos) e emenda um no outro.
    Devolve (grafo, saída de imagem, saída de som). Sem nada a tirar, o grafo vem vazio e as saídas são as entradas."""
    if not (len(manter) > 1 or (manter and manter[0] != (0.0, bruto))):
        return "", "[0:v]", "[0:a]"
    n = len(manter)
    grafo = (f"[0:v]split={n}" + "".join(f"[sv{k}]" for k in range(n)) + ";"
             + f"[0:a]asplit={n}" + "".join(f"[sa{k}]" for k in range(n)) + ";")
    for k, (a, b) in enumerate(manter):
        grafo += (f"[sv{k}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[kv{k}];"
                  f"[sa{k}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[ka{k}];")
    grafo += "".join(f"[kv{k}][ka{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac];"
    return grafo, "[vc]", "[ac]"


def renderizar(video: Path, info: ffmpeg.Info, inicio: float, fim: float, palavras: list[dict], titulo: str,
               saida: Path, layout: str, com_legenda: bool, marca: str = "",
               abertura: Fala | None = None, fecho: Fala | None = None, frases: list[dict] | None = None,
               gancho: str = "", apertar: bool = True, camera: str = "") -> float:
    """Renderiza o corte e devolve a duração final.

    Com narração, o vídeo fica parado enquanto o narrador fala: no primeiro quadro durante a `abertura` e no
    último durante o `fecho`, sem o som original, para as duas vozes nunca se misturarem.
    `frases` são as falas do corte já traduzidas (mesmo relógio de `palavras`), para a segunda linha da legenda.
    `gancho` é a frase que abre o corte na tela. Com `apertar`, os silêncios do corte são encurtados.
    """
    if not info.tem_audio:
        abertura = fecho = None
    bruto = fim - inicio  # o trecho do vídeo original; `clipe` é o que sobra dele depois de tirar os silêncios
    tirar = silencios(video, inicio, bruto) if apertar and info.tem_audio else []
    manter = sobra(tirar, bruto)
    clipe = sum(b - a for a, b in manter)

    def no_corte(t: float) -> float:
        """Leva um instante do trecho original para o relógio do corte já sem os silêncios."""
        return t - sum(min(max(t - a, 0.0), b - a) for a, b in tirar)

    pre = abertura.duracao + 0.4 if abertura else 0.0
    pos = fecho.duracao + 0.6 if fecho else 0.0
    duracao = pre + clipe + pos
    t_abertura, t_fecho = 0.15, pre + clipe + 0.25  # quando cada fala começa

    geo = geometria(info, layout, ler_camera(camera))
    fps = min(round(info.fps) or 30, 60)
    filtro, pausa = geo.filtro, ""
    emenda, _, som = emendar(manter, bruto)  # corta cada pedaço que fica e emenda um no outro
    if emenda:
        filtro = filtro.replace("[0:v]", "[vc]", 1)
    if pre or pos:
        entrada = "[vc]" if emenda else "[0:v]"
        filtro = (f"{entrada}tpad=start_mode=clone:start_duration={pre:.3f}:stop_mode=clone:stop_duration={pos:.3f}[vp];"
                  + filtro.replace(entrada, "[vp]", 1))
        # a imagem parada escurece um pouco, para ficar claro que o vídeo está em pausa
        pausa = (f"drawbox=x=0:y=0:w=iw:h=ih:color=black@{ESCURO}:t=fill:"
                 f"enable='lt(t,{pre:.3f})+gte(t,{pre + clipe:.3f})',")

    ass = saida.parent / "_legenda.ass"  # nome fixo: o título do corte pode ter caracteres que quebram o filtro
    legenda: list[dict] = []
    traducoes: list[dict] = []
    if com_legenda:
        legenda = [{"i": no_corte(max(0.0, p["i"] - inicio)) + pre, "f": no_corte(p["f"] - inicio) + pre, "t": p["t"]}
                   for p in palavras if p["i"] >= inicio - 0.05 and p["f"] <= fim + 0.05]
        traducoes = [{"i": no_corte(max(0.0, f["i"] - inicio)) + pre,
                      "f": no_corte(min(f["f"] - inicio, bruto)) + pre, "t": f["t"]}
                     for f in frases or [] if f["t"]]
        if abertura:
            legenda = abertura.palavras(t_abertura) + legenda
            if abertura.traducao:
                traducoes.insert(0, {"i": t_abertura, "f": pre - 0.05, "t": abertura.traducao})
        if fecho:
            legenda += fecho.palavras(t_fecho)
            if fecho.traducao:
                traducoes.append({"i": t_fecho, "f": duracao - 0.1, "t": fecho.traducao})
    legendas.gerar(ass, legenda, titulo, duracao, geo.y_legenda, geo.titulo_pos,
                   duracao if geo.titulo_fixo else 4.0, marca, traducoes, gancho)

    entradas = ["-ss", f"{inicio:.3f}", "-t", f"{bruto:.3f}", "-i", str(video.resolve())]
    grafo = f"{emenda}{filtro};[v0]{pausa}ass={ass.name}[v]"
    mapas = ["-map", "[v]"]
    if info.tem_audio:
        original = f"{som}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo"
        falas = [(f, t) for f, t in ((abertura, t_abertura), (fecho, t_fecho)) if f]
        if falas:
            # o som original some junto com a imagem parada: só o narrador fala antes e depois do corte
            original += f",afade=t=in:d=0.1,afade=t=out:st={max(0.0, clipe - 0.2):.2f}:d=0.2"
            if pre:
                original += f",adelay={pre * 1000:.0f}:all=1"
            original += f",apad=whole_dur={duracao:.3f}[orig]"
            grafo += ";" + original
            for n, (fala, comeco) in enumerate(falas, start=1):
                entradas += ["-i", str(fala.arquivo.resolve())]
                grafo += (f";[{n}:a]loudnorm=I=-13:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo,"
                          f"adelay={comeco * 1000:.0f}:all=1[fala{n}]")
            mistura = "[orig]" + "".join(f"[fala{n}]" for n in range(1, len(falas) + 1))
            grafo += f";{mistura}amix=inputs={len(falas) + 1}:normalize=0:duration=first,alimiter=limit=0.9"
        else:
            grafo += ";" + original
        grafo += f",afade=t=in:d=0.1,afade=t=out:st={max(0.0, duracao - 0.25):.2f}:d=0.25[a]"
        mapas += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ac", "2"]

    try:
        # roda na pasta de saída para o filtro `ass` receber só o nome do arquivo (sem escapar C:\...)
        ffmpeg.rodar([*entradas, "-filter_complex", grafo, *mapas, "-t", f"{duracao:.3f}", "-r", str(fps), *codec(),
                      "-pix_fmt", "yuv420p", "-movflags", "+faststart", saida.name], cwd=saida.parent)
    finally:
        ass.unlink(missing_ok=True)
    return duracao
