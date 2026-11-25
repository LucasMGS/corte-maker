"""Compilação temática: junta cortes que já estão na biblioteca num vídeo horizontal mais longo, com um tema único,
para publicar como vídeo normal do YouTube.

Duas etapas separadas, porque só a primeira gasta IA:
  1. `propor` lê todos os manifestos de `saida/`, manda a lista de cortes ao Claude e guarda os temas que ele propõe;
  2. `gerar` monta o tema escolhido: cada momento em 1920x1080, uma cartela entre um e outro, miniatura e ficha.

Formato de cada momento, do melhor para o pior material disponível:
  - o trecho original baixado de novo pelo link (16:9 de verdade, sem nada queimado na imagem). Só vale para cortes
    de vídeos gravados: num corte de live, `inicio` e `fim` contam do começo do monitoramento, não do começo da live,
    então não dá para achar o trecho na gravação que a plataforma publica depois;
  - a faixa do corte vertical onde está o vídeo (enquadramentos "inteiro" e "aproximado"), recortada e ampliada para
    a tela toda: some o fundo desfocado, o título e a legenda vertical;
  - sem faixa para recortar (enquadramento "tela cheia"), o corte vertical inteiro sobre fundo desfocado.

Do corte pronto entra só o vídeo: as falas do narrador, antes e depois dele, ficam de fora (ver `miolo`). Numa
compilação os momentos se emendam um no outro, e um comentário com pergunta ao público no fim de cada um quebra o ritmo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import audio, baixar, editar, ffmpeg, gasto, ia, legendas, pipeline, progresso
from .pipeline import MARCA, RAIZ, nome_arquivo

MANIFESTO = "compilacao.json"
PREFIXO = "COMPILAÇÃO - "
L, A = 1920, 1080
MIN_CORTES, MAX_CORTES = 4, 12  # quantos cortes cabem numa compilação
MAX_HASHTAGS = 5  # o YouTube recomenda de 3 a 5 por vídeo; as 3 primeiras aparecem acima do título
LIMITE_LISTA = 200  # quantos cortes da biblioteca vão para a IA, no máximo (os de nota mais alta)
CARTELA_S = 2.2  # segundos de cada cartela entre dois momentos
GUARDADOS = RAIZ / "trabalho" / "compilacoes" / "temas.json"  # a última proposta de temas, para não pedir de novo


# ---------------------------------------------------------------- biblioteca de cortes

def todos(saida: Path) -> list[dict]:
    """Todos os cortes prontos de `saida`, cada um com o que a sessão dele sabe (canal, link, enquadramento)."""
    cortes = []
    for arquivo in sorted(saida.glob(f"*/{pipeline.MANIFESTO}")):
        pasta = arquivo.parent
        sessao = pipeline.ler_manifesto(pasta)
        if sessao is None:
            continue
        for c in sessao.get("cortes", []):
            nome = str(c.get("arquivo", ""))
            if not nome or not (pasta / nome).is_file():
                continue
            inicio, fim = float(c.get("inicio") or 0), float(c.get("fim") or 0)
            cortes.append({
                "id": f"{pasta.name}/{nome}", "pasta": pasta.name, "arquivo": nome, "capa": str(c.get("capa", "")),
                "titulo": str(c.get("titulo") or Path(nome).stem), "descricao": str(c.get("descricao", "")),
                "hashtags": [str(h) for h in c.get("hashtags") or []], "motivo": str(c.get("motivo", "")),
                "nota": int(c.get("nota") or 0), "duracao": float(c.get("duracao") or max(0.0, fim - inicio)),
                "inicio": inicio, "fim": fim, "abertura": str(c.get("abertura", "")), "fecho": str(c.get("fecho", "")),
                "canal": str(sessao.get("canal", "")), "url": str(sessao.get("url", "")),
                "fonte": str(sessao.get("titulo", "")), "layout": str(c.get("layout") or sessao.get("layout", "")),  # o corte pode ter sido refeito
                "de_live": "ao_vivo" in sessao or pasta.name.startswith("AO VIVO - "),
            })
    return cortes


def biblioteca(saida: Path, cortes: list[dict] | None = None) -> list[dict]:
    """Os cortes que podem entrar numa compilação, do melhor para o pior. Quando o mesmo momento foi cortado mais
    de uma vez (vídeo refeito, live com dois monitoramentos), fica só a versão de nota mais alta."""
    escolhidos: list[dict] = []
    for c in sorted(todos(saida) if cortes is None else cortes, key=lambda c: (-c["nota"], c["id"])):
        def mesmo(o: dict) -> bool:
            if (o["url"], o["fonte"]) != (c["url"], c["fonte"]):
                return False
            junto = min(o["fim"], c["fim"]) - max(o["inicio"], c["inicio"])
            return junto > 0.5 * min(o["fim"] - o["inicio"], c["fim"] - c["inicio"])
        if not any(mesmo(o) for o in escolhidos):
            escolhidos.append(c)
    return escolhidos


def prontas(saida: Path) -> list[dict]:
    """As compilações já geradas, da mais nova para a mais antiga."""
    feitas = []
    for arquivo in saida.glob(f"*/{MANIFESTO}"):
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(dados, dict) and (arquivo.parent / str(dados.get("arquivo", ""))).is_file():
            dados["pasta"] = arquivo.parent.name
            feitas.append(dados)
    return sorted(feitas, key=lambda d: d.get("criado_em", 0), reverse=True)


# ---------------------------------------------------------------- escolha do tema (IA)

def _digital(cortes: list[dict]) -> str:
    """Muda quando a biblioteca muda: é assim que se sabe se a proposta guardada ainda vale."""
    return hashlib.sha1("\n".join(sorted(c["id"] for c in cortes)).encode()).hexdigest()[:12]


def _prompt(cortes: list[dict], pedido: str, feitas: list[str], marca: str) -> str:
    linhas = "\n".join(
        f"[{n}] nota {c['nota']} | {c['duracao']:.0f}s | {c['canal'] or 'canal desconhecido'} | {c['titulo']} — "
        f"{c['descricao']} | {' '.join('#' + h for h in c['hashtags'])} | por quê: {c['motivo']}"
        for n, c in enumerate(cortes, start=1))
    extras = ""
    if pedido:
        extras += (f"\nO dono do canal pediu este tema: {pedido}\nProponha de 1 a 3 compilações dentro desse pedido. "
                   "Se não houver cortes suficientes para ele, devolva a lista vazia.\n")
    if feitas:
        extras += "\nCompilações que o canal já fez (não repita o tema):\n" + "\n".join(f"- {t}" for t in feitas[:30]) + "\n"
    return f"""O canal {marca or "de cortes"} publica cortes curtos de lives e vídeos de games. Abaixo está a biblioteca de cortes prontos, um por linha, no formato:
[número] nota de 0 a 100 | duração | canal de origem | título — descrição | hashtags | por quê: motivo da escolha

Monte compilações com esses cortes para publicar como vídeo normal do YouTube (horizontal, de vários minutos). O YouTube recomenda compilações focadas num tema único, com narrativa clara, em vez de clipes aleatórios.

Regras:
- Cada compilação tem um tema único e específico, que todos os cortes dela compartilham de verdade. Exemplos de tema: bugs e bizarrices do mapa, primeiras reações ao jogo, encrencas com a polícia, detalhes que ninguém esperava. "Melhores momentos" não é tema.
- De {MIN_CORTES} a {MAX_CORTES} cortes por compilação. Sem pelo menos {MIN_CORTES} cortes que caibam de verdade, não proponha o tema. Poucas compilações boas valem mais que muitas forçadas.
- Não repita um corte em duas compilações, a não ser que ele caiba muito bem nas duas.
- Nunca ponha na mesma compilação dois cortes que mostram o mesmo momento.
- Não misture jogos sem relação nem idiomas: todos os cortes de uma compilação no mesmo idioma.
- Ordem: o corte mais forte primeiro, para segurar quem acabou de chegar; o segundo mais forte por último, para fechar bem; no meio, alterne canais e tipos de momento para o ritmo não cair.

Para cada compilação devolva:
- tema: o tema em até 5 palavras (vira nome de pasta).
- titulo: título do vídeo no YouTube, com até 70 caracteres, no idioma dos cortes. Diz o que a pessoa vai ver e desperta curiosidade, sem tudo em maiúsculas. O nome do jogo (ou do streamer) e o gancho ficam nos primeiros 40 caracteres, porque o celular corta o título por aí. Só cite pelo nome o que aparece de verdade nos cortes escolhidos: o YouTube compara o título com o vídeo e esconde o que promete e não entrega. Se usar número ("7 vezes que..."), é o número real de cortes.
- titulos_ab: 2 títulos alternativos para o teste A/B de título do YouTube, com as mesmas regras do principal e estratégias diferentes dele e entre si: um de busca (direto, com o nome do jogo e do assunto na frente, como alguém digitaria) e um de curiosidade (uma emoção, uma pergunta ou um contraste que dá vontade de clicar). Não vale o mesmo título com palavras trocadas.
- descricao: 2 ou 3 frases para a descrição do vídeo, com as palavras que alguém buscaria. A primeira frase traz o nome do jogo e funciona sozinha: é só ela que aparece antes do "mostrar mais". Sem hashtags, créditos nem capítulos: isso é acrescentado depois.
- hashtags: de 3 a {MAX_HASHTAGS}, sem o #, da mais específica para a mais ampla (as 3 primeiras aparecem acima do título).
- cortes: os números dos cortes, na ordem em que entram no vídeo.
- corte_capa: o número do corte, entre os escolhidos, que tem a imagem mais forte para a miniatura.
- texto_capa: até 3 palavras para escrever grande na miniatura, que completem o título em vez de repeti-lo.
- motivo: uma frase dizendo por que esse conjunto funciona como vídeo.
{extras}
Biblioteca:
{linhas}"""


def _validar(bruto: list[dict], cortes: list[dict]) -> list[dict]:
    """Troca os números da IA pelos cortes de verdade e descarta o grupo que não fecha o mínimo."""
    temas = []
    for t in bruto:
        numeros: list[int] = []
        for n in t.get("cortes") or []:
            if isinstance(n, int) and 1 <= n <= len(cortes) and n not in numeros:
                numeros.append(n)
        numeros = numeros[:MAX_CORTES]
        titulo = " ".join(str(t.get("titulo", "")).split())[:100]
        if len(numeros) < MIN_CORTES or not titulo:
            continue
        alternativos: list[str] = []
        for outro in t.get("titulos_ab") or []:
            outro = " ".join(str(outro).split())[:100]
            if outro and outro.casefold() not in {x.casefold() for x in (titulo, *alternativos)}:
                alternativos.append(outro)
        capa = t.get("corte_capa") if t.get("corte_capa") in numeros else numeros[0]
        escolhidos = [cortes[n - 1] for n in numeros]
        temas.append({
            "tema": " ".join(str(t.get("tema", "")).split())[:60] or titulo[:60], "titulo": titulo,
            "titulos_ab": alternativos[:2],
            "descricao": " ".join(str(t.get("descricao", "")).split()),
            "hashtags": [h for h in (str(x).strip().lstrip("#").replace(" ", "") for x in t.get("hashtags") or []) if h][:MAX_HASHTAGS],
            "texto_capa": " ".join(str(t.get("texto_capa", "")).split())[:40],
            "motivo": " ".join(str(t.get("motivo", "")).split()),
            "cortes": [c["id"] for c in escolhidos], "capa": cortes[capa - 1]["id"],
            "duracao": _previsto(escolhidos),
        })
    return temas


def _previsto(cortes: list[dict]) -> int:
    """Duração aproximada da compilação desses cortes, em segundos: sem as falas do narrador, com as cartelas."""
    videos = sum(min(c["duracao"], c["fim"] - c["inicio"]) if c["fim"] > c["inicio"] else c["duracao"] for c in cortes)
    return round(videos + CARTELA_S * (len(cortes) - 1))


def guardados(saida: Path, cortes: list[dict] | None = None) -> dict | None:
    """A última proposta de temas, com `atual` dizendo se a biblioteca continua a mesma de quando ela foi feita.
    `cortes` é o resultado de `todos(saida)`, para quem já o tem não ler os manifestos de novo."""
    try:
        dados = json.loads(GUARDADOS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(dados, dict) or dados.get("saida") != str(saida.resolve()) or not isinstance(dados.get("temas"), list):
        return None
    cortes = todos(saida) if cortes is None else cortes
    dados["atual"] = dados.get("biblioteca") == _digital(biblioteca(saida, cortes)[:LIMITE_LISTA])
    por_id = {c["id"]: c for c in cortes}
    for tema in dados["temas"]:  # a conta da duração pode ter mudado desde que a proposta foi guardada
        if isinstance(tema, dict) and isinstance(tema.get("cortes"), list):
            tema["duracao"] = _previsto([por_id[i] for i in tema["cortes"] if i in por_id])
    return dados


def propor(saida: Path, pedido: str = "", modelo: str | None = None, marca: str = MARCA) -> dict:
    """Pede os temas à IA (uma chamada) e guarda a resposta. `pedido` é o tema que o dono do canal quer, se quiser um."""
    cortes = biblioteca(saida)[:LIMITE_LISTA]
    if len(cortes) < MIN_CORTES:
        raise ValueError(f"a biblioteca tem {len(cortes)} corte(s); uma compilação precisa de pelo menos {MIN_CORTES}")
    conta = gasto.abrir()
    pedido = " ".join(pedido.split())[:300]
    try:
        bruto = ia.pedir_temas(_prompt(cortes, pedido, [p.get("titulo", "") for p in prontas(saida)], marca), modelo)
    finally:
        if conta.resumo()["chamadas_ia"]:  # a chamada é cobrada mesmo quando a resposta não serve
            gasto.registrar(conta, "temas", f"temas de compilação ({len(cortes)} cortes lidos)", "", 0, 0)
    dados = {"criado_em": time.time(), "saida": str(saida.resolve()), "pedido": pedido, "biblioteca": _digital(cortes),
             "n_cortes": len(cortes), "temas": _validar(bruto, cortes), "gasto": conta.resumo()}
    GUARDADOS.parent.mkdir(parents=True, exist_ok=True)
    _gravar(GUARDADOS, dados)
    progresso.info(conta.frase())
    return dados | {"atual": True}


def _gravar(arquivo: Path, dados: dict) -> None:
    tmp = arquivo.with_name(arquivo.name + ".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(arquivo)


# ---------------------------------------------------------------- material de cada momento

@dataclass
class Peca:
    """De onde sai um momento da compilação."""
    corte: dict
    video: Path  # o trecho original baixado de novo ou o corte vertical pronto
    inicio: float  # o pedaço de `video` que entra
    fim: float
    recorte: tuple[int, int] | None  # (topo, altura) da faixa do corte vertical onde está o vídeo; None: o quadro todo
    original: bool
    info: ffmpeg.Info


def faixa(video: Path, info: ffmpeg.Info, layout: str = "") -> tuple[int, int] | None:
    """Onde está o vídeo dentro de um corte vertical montado sobre fundo desfocado: (topo, altura), ou None se o
    corte ocupa o quadro todo.

    A faixa fica sempre centrada na altura (ver `editar.geometria`), e as bordas dela são as únicas linhas que
    atravessam a largura inteira, nos mesmos lugares, o corte todo: logo acima e logo abaixo o fundo desfocado
    quase não muda de uma linha para a outra. O enquadramento do manifesto ajuda quando existe, mas manifestos
    refeitos pelas fichas não o trazem, e a altura da faixa depende da proporção do vídeo original."""
    if layout in ("cheio", "camera") or (info.largura, info.altura) != (editar.L, editar.A):
        return None
    if layout == "blur":
        return 555, 810  # o "aproximado" corta as laterais até 4:3: a faixa é sempre esta
    larg, n = 270, 10
    cru = ffmpeg.saida(["-ss", "1", "-i", str(video), "-vf",
                        f"fps={n / max(info.duracao - 2, 1):.4f},scale={larg}:{editar.A}:flags=area,format=gray",
                        "-frames:v", str(n), "-f", "rawvideo", "-"])
    quadros = np.frombuffer(cru, dtype=np.uint8)
    quadros = quadros[: len(quadros) // (larg * editar.A) * larg * editar.A].reshape(-1, editar.A, larg).astype(np.float32)
    if len(quadros):
        salto = np.median(np.abs(np.diff(quadros, axis=1)), axis=2).mean(axis=0)  # mudança de cada linha para a seguinte
        meio = editar.A // 2
        meias = np.arange(100, meio - 60)  # metade da altura da faixa, em linhas
        borda = np.minimum(salto[meio - meias - 1], salto[meio + meias - 1])
        fora = np.maximum(salto[meio - meias - 3], salto[meio + meias + 1])
        nota = borda - 3 * fora
        bons = np.flatnonzero(nota >= max(3.0, 0.4 * float(nota.max())))
        if len(bons):
            meia = int(meias[bons[-1]])  # a mais de fora: uma borda parecida mais para dentro é do próprio vídeo
            return meio - meia, 2 * meia
    return (656, 608) if layout in ("inteiro", "preto") else None  # sem achar (corte muito escuro): o caso comum, vídeo 16:9


PASSO = 0.05  # de quanto em quanto tempo `miolo` olha a imagem, em segundos
PARADO = 2.0  # até quanto dois quadros da mesma imagem parada diferem (níveis de cinza, em média), pela compressão


def miolo(video: Path, info: ffmpeg.Info, recorte: tuple[int, int] | None,
          abertura: bool = False, fecho: bool = False) -> tuple[float, float]:
    """O pedaço de um corte pronto que é o vídeo de verdade, sem as falas do narrador: (início, fim), em segundos.

    Enquanto o narrador fala, antes ou depois do corte, a imagem fica parada e 40% mais escura, e o som original
    some (ver `editar.renderizar`). O manifesto guarda o texto da fala, mas não quanto ela dura, então o lugar sai
    da própria imagem: do fim para trás, até onde os quadros são iguais ao último, e do começo para a frente, até
    onde são iguais ao primeiro. Só conta como fala se a imagem também escurece nessa hora, ou se o manifesto diz
    que o corte tem a fala (`abertura`, `fecho`): um corte que termina numa tela parada continua inteiro.
    Olha só o alto da faixa do vídeo, onde a legenda da fala, que muda a cada palavra, não chega."""
    topo, altura = (recorte[0] + 4, int(recorte[1] * 0.6)) if recorte else (int(info.altura * 0.30), int(info.altura * 0.28))
    larg, alt = 96, 54
    cru = ffmpeg.saida(["-i", str(video), "-vf",
                        f"crop=iw:{altura}:0:{topo},fps={1 / PASSO:.0f},scale={larg}:{alt}:flags=area,format=gray",
                        "-f", "rawvideo", "-"])
    q = np.frombuffer(cru, dtype=np.uint8)
    q = q[: len(q) // (larg * alt) * larg * alt].reshape(-1, alt, larg).astype(np.float32)
    inicio, fim = 0.0, info.duracao
    minimo = int(1.0 / PASSO)  # uma fala, com as folgas, nunca dura menos de um segundo
    if len(q) < 4 * minimo:
        return inicio, fim
    luz = q.mean(axis=(1, 2))
    quadro = 1 / max(info.fps or 30, 1 / PASSO)  # folga de um quadro do vídeo, para nenhum quadro parado entrar
    mexe = np.flatnonzero(np.abs(q - q[-1]).mean(axis=(1, 2)) > PARADO)
    k = int(mexe[-1]) + 1 if len(mexe) else 0  # o primeiro quadro da imagem parada do fim
    if k and len(q) - k >= minimo and (fecho or luz[k] < 0.88 * luz[k - 1]):
        fim = (k - 0.5) * PASSO - quadro
    mexe = np.flatnonzero(np.abs(q - q[0]).mean(axis=(1, 2)) > PARADO)
    j = int(mexe[0]) if len(mexe) else len(q)  # o primeiro quadro depois da imagem parada do começo
    if minimo <= j < len(q) and (abertura or luz[j - 1] < 0.88 * luz[j]):
        inicio = (j + 0.5) * PASSO + quadro
    return (inicio, fim) if fim - inicio >= 3 else (0.0, info.duracao)


def _original(c: dict, n: int, pronto: Path, trabalho: Path, consultas: dict) -> Peca:
    """Baixa de novo o trecho do vídeo original e acha, pelo som, onde o corte começa dentro dele."""
    url = c["url"]
    if url not in consultas:
        try:
            consultas[url] = baixar.consultar(url)
        except Exception as erro:
            consultas[url] = erro
    dados = consultas[url]
    if isinstance(dados, Exception):
        raise dados
    if dados.get("is_live"):
        raise RuntimeError("o link agora aponta para uma live no ar")
    pedido = max(0.0, c["inicio"] - pipeline.FOLGA)
    bruto = baixar.trecho(dados, pedido, c["fim"] + pipeline.FOLGA, trabalho / f"original_{n:02d}")
    info = ffmpeg.info(bruto)
    comeco = c["inicio"] - pedido
    if info.tem_audio and not c["abertura"]:  # com narrador na abertura, o corte pronto não começa pelo som original
        wav = trabalho / f"original_{n:02d}.wav"
        audio.extrair_wav(bruto, wav)
        comeco = audio.alinhar(wav, pronto, comeco)
        wav.unlink(missing_ok=True)
    fim = min(comeco + (c["fim"] - c["inicio"]), info.duracao)
    if fim - comeco < 5:
        raise RuntimeError("o trecho baixado veio curto demais")
    return Peca(c, bruto, comeco, fim, None, True, info)


def _peca(c: dict, n: int, saida: Path, trabalho: Path, so_prontos: bool, consultas: dict, narrador: bool) -> Peca:
    pronto = saida / c["pasta"] / c["arquivo"]
    if not so_prontos and not c["de_live"] and baixar.eh_link(c["url"]):
        try:
            return _original(c, n, pronto, trabalho, consultas)
        except Exception as erro:  # link fora do ar, vídeo apagado, rede: o corte pronto resolve
            motivo = (str(erro).strip().splitlines() or [type(erro).__name__])[-1][:160]
            progresso.aviso(f"Original de \"{c['titulo']}\" indisponível ({motivo}). Usando o corte vertical pronto.")
    info = ffmpeg.info(pronto)
    recorte = faixa(pronto, info, c["layout"])
    inicio, fim = (0.0, info.duracao) if narrador else miolo(pronto, info, recorte, bool(c["abertura"]), bool(c["fecho"]))
    return Peca(c, pronto, inicio, fim, recorte, False, info)


# ---------------------------------------------------------------- montagem

def _enquadrar(entrada: str, p: Peca) -> str:
    """Filtergraph que leva `entrada` até [v0] em 1920x1080: a imagem enche a tela quando já é 16:9 e, quando
    não é, entra inteira sobre o fundo desfocado dela mesma."""
    largura, altura, recorte = p.info.largura, p.info.altura, ""
    if p.recorte:
        topo, altura = p.recorte[0] + 2, p.recorte[1] - 4  # dois pixels para dentro: a borda da faixa não entra
        recorte = f"crop={largura}:{altura}:0:{topo},"
    if abs(largura / altura - L / A) < 0.04:
        return f"{entrada}{recorte}scale={L}:{A}:flags=lanczos,setsar=1[v0]"
    return (f"{entrada}{recorte}split=2[f][t];"
            f"[f]scale=480:270:force_original_aspect_ratio=increase,crop=480:270,gblur=sigma=10,"
            f"scale={L}:{A}:flags=bilinear,eq=brightness=-0.12[fundo];"
            f"[t]scale={L}:{A}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos[frente];"
            f"[fundo][frente]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")


def _codec(fps: int) -> list[str]:
    """Todos os pedaços saem com exatamente os mesmos parâmetros, para serem emendados sem recodificar a imagem."""
    if ffmpeg.tem_nvenc():
        video = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0",
                 "-maxrate", "16M", "-bufsize", "32M"]
    else:
        video = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19"]
    return [*video, "-g", str(fps * 2), "-r", str(fps), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2", "-f", "mpegts"]


def _clipe(p: Peca, numero: int, destino: Path, fps: int, marca: str) -> None:
    """Um momento em 1920x1080, com o número e o título no canto nos primeiros segundos e a assinatura discreta."""
    bruto = p.fim - p.inicio
    # o corte pronto já teve os silêncios tirados e o som normalizado; o trecho original passa por isso agora
    tirar = editar.silencios(p.video, p.inicio, bruto) if p.original and p.info.tem_audio else []
    manter = editar.sobra(tirar, bruto)
    clipe = sum(b - a for a, b in manter)
    emenda, imagem, som = editar.emendar(manter, bruto)

    ass = destino.parent / "_rotulo.ass"
    c = p.corte
    assinatura = "  ·  ".join(x for x in (marca, f"créditos: {c['canal']}" if c["canal"] else "") if x)
    legendas.rotulo(ass, numero, c["titulo"], assinatura, clipe)
    entradas = ["-ss", f"{p.inicio:.3f}", "-t", f"{bruto:.3f}", "-i", str(p.video.resolve())]
    grafo = f"{emenda}{_enquadrar(imagem, p)};[v0]ass={ass.name}[v]"
    if p.info.tem_audio:
        normalizar = "loudnorm=I=-14:TP=-1.5:LRA=11," if p.original else ""
        grafo += (f";{som}{normalizar}aresample=48000,aformat=channel_layouts=stereo,afade=t=in:d=0.08,"
                  f"afade=t=out:st={max(0.0, clipe - 0.25):.2f}:d=0.25[a]")
        mapas = ["-map", "[v]", "-map", "[a]"]
    else:
        entradas += ["-f", "lavfi", "-t", f"{clipe:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        mapas = ["-map", "[v]", "-map", "1:a"]
    try:
        ffmpeg.rodar([*entradas, "-filter_complex", grafo, *mapas, "-t", f"{clipe:.3f}", *_codec(fps), destino.name],
                     cwd=destino.parent)
    finally:
        ass.unlink(missing_ok=True)


def _cartela(numero: int, c: dict, clipe: Path, destino: Path, fps: int) -> None:
    """A cartela que anuncia o momento seguinte: número e título sobre um quadro dele, desfocado e escurecido."""
    ass = destino.parent / "_cartela.ass"
    legendas.cartela(ass, numero, c["titulo"], c["canal"], CARTELA_S)
    grafo = (f"[0:v]trim=end_frame=1,scale=480:270,gblur=sigma=14,scale={L}:{A}:flags=bilinear,"
             f"eq=brightness=-0.2:saturation=0.85,loop=loop=-1:size=1:start=0,fps={fps},ass={ass.name}[v]")
    try:
        ffmpeg.rodar(["-ss", "1.5", "-i", clipe.name, "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                      "-filter_complex", grafo, "-map", "[v]", "-map", "1:a", "-t", f"{CARTELA_S:.3f}",
                      *_codec(fps), destino.name], cwd=destino.parent)
    finally:
        ass.unlink(missing_ok=True)


def _melhor_quadro(p: Peca) -> float:
    """O instante de `p.video` com a imagem mais forte para a miniatura: nítida, contrastada e colorida, nem
    escura nem estourada, de preferência na hora em que o som sobe (o grito, a risada: é quando a cena e o rosto
    do streamer têm mais chance de dizer alguma coisa). Olha um quadro a cada segundo e meio, mais ou menos."""
    duracao = p.fim - p.inicio
    if not p.original:  # no fim do corte pronto a imagem fica parada e escura enquanto o narrador comenta
        duracao = min(duracao, max(5.0, p.corte["fim"] - p.corte["inicio"]))
    n = min(24, max(4, int(duracao / 1.5)))
    passo = duracao / (n + 1)
    recorte = f"crop=iw:{p.recorte[1] - 4}:0:{p.recorte[0] + 2}," if p.recorte else ""
    cru = ffmpeg.saida(["-ss", f"{p.inicio + passo:.3f}", "-t", f"{duracao - passo:.3f}", "-i", str(p.video), "-vf",
                        f"{recorte}fps={1 / passo:.5f},scale=160:90,format=rgb24", "-frames:v", str(n), "-f", "rawvideo", "-"])
    q = np.frombuffer(cru, dtype=np.uint8)
    q = q[: len(q) // (160 * 90 * 3) * 160 * 90 * 3].reshape(-1, 90, 160, 3).astype(np.float32)
    if len(q) == 0:
        return p.inicio + duracao / 2
    luz = q.mean(axis=3)
    z = lambda v: (v - v.mean()) / (v.std() + 1e-6)
    nota = (z(luz.std(axis=(1, 2))) + z((q.max(axis=3) - q.min(axis=3)).mean(axis=(1, 2)))
            + z(np.abs(np.diff(luz, axis=2)).mean(axis=(1, 2))))
    brilho = luz.mean(axis=(1, 2))
    nota[(brilho < 45) | (brilho > 210)] -= 3
    if p.info.tem_audio:
        som = np.frombuffer(ffmpeg.saida(["-ss", f"{p.inicio:.3f}", "-t", f"{duracao:.3f}", "-i", str(p.video), "-vn",
                                          "-ac", "1", "-ar", str(audio.TAXA), "-f", "s16le", "-"]), dtype=np.int16)
        volume = np.zeros(len(q), dtype=np.float32)
        for k in range(len(q)):  # o volume em volta de cada quadro
            meio = int(passo * (k + 1) * audio.TAXA)
            janela = som[max(0, meio - audio.TAXA // 2): meio + audio.TAXA // 2].astype(np.float32)
            volume[k] = 20 * np.log10(np.sqrt((janela ** 2).mean()) + 1.0) if len(janela) else 0.0
        nota += z(volume)
    return p.inicio + passo * (int(nota.argmax()) + 1)


def _quadro(p: Peca, pasta: Path, destino: Path, texto: str = "", instante: float | None = None) -> None:
    """Grava em `destino` o quadro mais forte de `p` (ou o do `instante` pedido, em segundos de `p.video`), em 1920x1080 (o tamanho que o YouTube recomenda, por causa
    das TVs), com o `texto` em letras grandes no canto."""
    tmp, ass = pasta / "_miniatura.jpg", pasta / "_miniatura.ass"  # nomes fixos, sem nada que o ffmpeg leia como padrão
    escrita = ""
    if texto:
        legendas.capa(ass, texto)
        escrita = f",ass={ass.name}"
    try:
        ffmpeg.rodar(["-ss", f"{_melhor_quadro(p) if instante is None else instante:.3f}", "-i", str(p.video.resolve()), "-filter_complex",
                      _enquadrar("[0:v]", p) + ";[v0]eq=contrast=1.06:saturation=1.2,"
                      f"unsharp=5:5:0.6{escrita}[q]", "-map", "[q]", "-frames:v", "1", "-update", "1", "-q:v", "2",
                      tmp.name], cwd=pasta)
        tmp.replace(destino)
    finally:
        ass.unlink(missing_ok=True)


def _miniaturas(pecas: list[Peca], da_capa: Peca, texto: str, pasta: Path) -> tuple[str, str, list[str]]:
    """Miniatura 1920x1080: um quadro forte do corte escolhido, com poucas palavras grandes no canto. Sai também
    sem o texto, para quem preferir escrever outra coisa por cima, e com até duas opções tiradas de outros
    momentos: o programa escolhe o quadro por nitidez, cor e volume, e quem vê escolhe melhor."""
    principal, limpa = pasta / "miniatura.jpg", pasta / "miniatura sem texto.jpg"
    _quadro(da_capa, pasta, principal, texto)
    if texto:
        _quadro(da_capa, pasta, limpa)
    else:
        limpa.write_bytes(principal.read_bytes())
    opcoes = []
    outras = sorted((p for p in pecas if p is not da_capa and (p.original or p.recorte)), key=lambda p: -p.corte["nota"])
    for n, p in enumerate(outras[:2], start=2):
        opcoes.append(pasta / f"miniatura opção {n}.jpg")
        _quadro(p, pasta, opcoes[-1], texto)
    return principal.name, limpa.name, [o.name for o in opcoes]


# ---------------------------------------------------------------- miniatura escolhida por quem olha
# O programa escolhe o quadro por nitidez, cor e volume, sem ver o que está na imagem. Estas duas funções deixam quem
# enxerga (uma pessoa, ou um agente que lê imagens) escolher: `folhas` mostra os quadros de cada momento e
# `miniatura` grava o quadro escolhido com o texto.

def _do_momento(pasta: Path, momento: int) -> tuple[dict, Peca]:
    """O manifesto da compilação de `pasta` e o corte pronto de onde saiu o momento número `momento` (a contar de 1)."""
    try:
        manifesto = json.loads((pasta / MANIFESTO).read_text(encoding="utf-8"))
        capitulos = manifesto["capitulos"]
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError(f"não achei uma compilação em {pasta}") from None
    if not 1 <= momento <= len(capitulos):
        raise RuntimeError(f"esta compilação tem os momentos de 1 a {len(capitulos)}")
    capitulo = capitulos[momento - 1]
    video = pasta.parent / capitulo["pasta"] / capitulo["arquivo"]
    if not video.is_file():
        raise RuntimeError(f"o corte do momento {momento} não está mais na biblioteca: {video}")
    info = ffmpeg.info(video)
    sessao = pipeline.ler_manifesto(video.parent) or {}
    item = next((c for c in sessao.get("cortes", []) if c.get("arquivo") == capitulo["arquivo"]), {})
    recorte = faixa(video, info, str(item.get("layout") or sessao.get("layout", "")))
    inicio, fim = miolo(video, info, recorte)
    return manifesto, Peca({"titulo": capitulo["titulo"], "inicio": 0.0, "fim": 0.0}, video, inicio, fim, recorte, False, info)


def folhas(pasta: Path, passo: float = 2.0) -> list[tuple[Path, str, int]]:
    """Uma folha de quadros por momento da compilação de `pasta`, para escolher a miniatura olhando: 4 quadros por
    linha, e o quadro k (a contar de 0, linha por linha) é o instante k * `passo` do momento. Devolve, por momento,
    (arquivo da folha, título, quantos quadros). As folhas ficam em `trabalho/miniaturas/<nome da pasta>/`."""
    destino = RAIZ / "trabalho" / "miniaturas" / pasta.name
    feitas, n = [], 1
    while True:
        try:
            _, p = _do_momento(pasta, n)
        except RuntimeError:
            if n == 1:
                raise
            return feitas
        destino.mkdir(parents=True, exist_ok=True)
        quantos = max(1, int((p.fim - p.inicio) / passo))
        folha, tmp = destino / f"momento {n:02d}.jpg", destino / "_folha.jpg"
        ffmpeg.rodar(["-ss", f"{p.inicio:.3f}", "-t", f"{quantos * passo:.3f}", "-i", str(p.video.resolve()), "-filter_complex",
                      _enquadrar("[0:v]", p) + f";[v0]fps={1 / passo:.5f},scale=480:270,tile=4x{-(-quantos // 4)}[q]",
                      "-map", "[q]", "-frames:v", "1", "-update", "1", "-q:v", "3", tmp.name], cwd=destino)
        tmp.replace(folha)
        feitas.append((folha, p.corte["titulo"], quantos))
        n += 1


def miniatura(pasta: Path, momento: int, tempo: float, texto: str = "", nome: str = "") -> Path:
    """Grava na compilação de `pasta` uma miniatura 1920x1080 com o quadro do instante `tempo` (segundos contados do
    começo do momento, como nas folhas de `folhas`) e o `texto` em letras grandes. Sem `nome`, vira a próxima
    "miniatura opção N.jpg"; com `nome` "miniatura.jpg", troca a principal."""
    manifesto, p = _do_momento(pasta, momento)
    if not 0 <= tempo <= p.fim - p.inicio:
        raise RuntimeError(f"o momento {momento} vai de 0 a {p.fim - p.inicio:.1f} s")
    nome = Path(nome).name if nome else ""
    if not nome:
        n = 2
        while (pasta / f"miniatura opção {n}.jpg").exists():
            n += 1
        nome = f"miniatura opção {n}.jpg"
    if not nome.lower().endswith(".jpg"):
        nome += ".jpg"
    _quadro(p, pasta, pasta / nome, " ".join(texto.split()), p.inicio + tempo)
    conhecidas = [manifesto.get("miniatura"), manifesto.get("miniatura_limpa"), *manifesto.get("miniaturas_opcoes", [])]
    if nome not in conhecidas:
        manifesto["miniaturas_opcoes"] = [*manifesto.get("miniaturas_opcoes", []), nome]
        _gravar(pasta / MANIFESTO, manifesto)
    return pasta / nome


def _relogio(t: float, com_horas: bool) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if com_horas else f"{t // 60}:{t % 60:02d}"


def _descricao(tema: dict, capitulos: list[dict], total: float) -> str:
    """O texto pronto para colar na descrição do YouTube: resumo, capítulos (o primeiro em 0:00, como o YouTube
    exige para criar os capítulos sozinho), créditos de cada canal e hashtags."""
    horas = total >= 3600
    partes = [tema["descricao"], "", "Momentos:"] if tema["descricao"] else ["Momentos:"]
    partes += [f"{_relogio(c['t'], horas)} {c['titulo']}" for c in capitulos]
    creditos: dict[str, str] = {}
    for c in capitulos:
        if c["canal"]:
            creditos.setdefault(c["canal"], c["url"])
    if creditos:
        partes += ["", "Créditos (os vídeos pertencem aos seus criadores):"]
        partes += [f"{canal}: {url}" if url else canal for canal, url in creditos.items()]
    if tema["hashtags"]:
        partes += ["", " ".join("#" + h for h in tema["hashtags"][:MAX_HASHTAGS])]
    return "\n".join(partes)


def titulos_para_teste(tema: dict) -> list[str]:
    """As linhas da ficha com os títulos para o teste A/B do YouTube Studio: o principal (A) e os alternativos."""
    outros = [t for t in tema.get("titulos_ab") or [] if t]
    if not outros:
        return []
    return ["", "TÍTULOS PARA O TESTE A/B (YouTube Studio, Testar e comparar)",
            *(f"{letra}: {t}" for letra, t in zip("ABC", [tema["titulo"], *outros]))]


def _pasta_nova(saida: Path, base: str) -> Path:
    pasta, n = saida / base, 1
    while pasta.exists():
        n += 1
        pasta = saida / f"{base} ({n})"
    pasta.mkdir(parents=True)
    return pasta


def gerar(tema: dict, saida: Path, so_prontos: bool = False, marca: str = MARCA, narrador: bool = False) -> dict:
    """Monta a compilação de um tema proposto e devolve o manifesto gravado em `compilacao.json`.
    Com `narrador`, os cortes prontos entram com as falas do narrador que têm; o padrão é deixá-las de fora."""
    t0 = time.time()
    por_id = {c["id"]: c for c in todos(saida)}
    cortes = [por_id[i] for i in tema["cortes"] if i in por_id]
    if len(cortes) < 2:
        raise RuntimeError("os cortes deste tema não estão mais na biblioteca; peça os temas de novo")
    if len(cortes) < len(tema["cortes"]):
        progresso.aviso(f"{len(tema['cortes']) - len(cortes)} corte(s) deste tema não estão mais na biblioteca e ficaram de fora.")
    progresso.titulo(tema["titulo"])
    trabalho = RAIZ / "trabalho" / f"compilacao_{uuid.uuid4().hex[:8]}"
    trabalho.mkdir(parents=True)
    pasta: Path | None = None
    try:
        progresso.etapa(1, 5, "Buscando o material de cada momento")
        pecas, consultas = [], {}
        for n, c in enumerate(cortes, start=1):
            pecas.append(_peca(c, n, saida, trabalho, so_prontos, consultas, narrador))
            progresso.pct(100 * n / len(cortes))
        originais = sum(p.original for p in pecas)
        progresso.info(f"{len(pecas)} momentos: {originais} do vídeo original, {len(pecas) - originais} do corte pronto.")
        fps = 60 if 2 * sum(p.info.fps >= 50 for p in pecas) >= len(pecas) else 30

        progresso.etapa(2, 5, "Montando os momentos em 1920x1080")
        clipes = []
        for n, p in enumerate(pecas, start=1):
            clipes.append(trabalho / f"clipe_{n:02d}.ts")
            _clipe(p, n, clipes[-1], fps, marca)
            progresso.pct(100 * n / len(pecas))
            progresso.info(f"{n:02d} | {'original' if p.original else 'faixa do corte' if p.recorte else 'corte inteiro':14} | "
                           f"{ffmpeg.duracao(clipes[-1]):4.0f}s | {p.corte['titulo']}")

        progresso.etapa(3, 5, "Fazendo as cartelas")
        partes, capitulos, t = [], [], 0.0
        for n, (p, clipe) in enumerate(zip(pecas, clipes), start=1):
            capitulos.append({"t": int(t), "titulo": p.corte["titulo"], "canal": p.corte["canal"], "url": p.corte["url"],
                              "pasta": p.corte["pasta"], "arquivo": p.corte["arquivo"],
                              "fonte": "original" if p.original else "corte"})
            if n > 1:  # o primeiro momento abre o vídeo direto, sem cartela: o começo é o que segura quem chegou
                cartela = trabalho / f"cartela_{n:02d}.ts"
                _cartela(n, p.corte, clipe, cartela, fps)
                partes.append(cartela)
                t += ffmpeg.duracao(cartela)
            partes.append(clipe)
            t += ffmpeg.duracao(clipe)
            progresso.pct(100 * n / len(pecas))

        progresso.etapa(4, 5, "Juntando tudo")
        pasta = _pasta_nova(saida, PREFIXO + nome_arquivo(tema["tema"] or tema["titulo"], 60))
        lista = trabalho / "lista.txt"
        lista.write_text("".join(f"file '{parte.name}'\n" for parte in partes), encoding="utf-8")
        video = pasta / f"{nome_arquivo(tema['titulo'])}.mp4"
        tmp = pasta / "_video.mp4"
        # a imagem é só copiada; o som é refeito numa faixa contínua, sem os buraquinhos entre um pedaço e outro
        ffmpeg.rodar(["-f", "concat", "-safe", "0", "-i", lista.name, "-map", "0:v", "-map", "0:a", "-c:v", "copy",
                      "-af", "aresample=async=1:first_pts=0", "-c:a", "aac", "-b:a", "192k", "-ac", "2",
                      "-movflags", "+faststart", str(tmp.resolve())], cwd=trabalho)
        tmp.replace(video)
        total = ffmpeg.duracao(video)

        progresso.etapa(5, 5, "Miniatura e ficha")
        da_capa = next((p for p in pecas if p.corte["id"] == tema.get("capa")), pecas[0])
        if not (da_capa.original or da_capa.recorte):  # corte inteiro na vertical: procura um com imagem horizontal limpa
            da_capa = next((p for p in pecas if p.original or p.recorte), da_capa)
        miniatura, limpa, opcoes = _miniaturas(pecas, da_capa, tema.get("texto_capa", ""), pasta)
        descricao = _descricao(tema, capitulos, total)
        usados = "\n".join(f"{n:02d} | {_relogio(c['t'], total >= 3600)} | {c['fonte']:8} | {c['pasta']}\\{c['arquivo']}"
                           for n, c in enumerate(capitulos, start=1))
        ficha = "\n".join([
            "TÍTULO", tema["titulo"], *titulos_para_teste(tema), "", "DESCRIÇÃO", descricao, "", "-" * 40,
            f"Texto da miniatura: {tema.get('texto_capa') or '(sem texto)'}",
            f"Miniaturas na pasta: {', '.join([miniatura, limpa, *opcoes])}",
            f"Duração: {_relogio(total, total >= 3600)} | {len(capitulos)} momentos | {L}x{A} a {fps} quadros por segundo",
            *([f"Por quê: {tema['motivo']}"] if tema.get("motivo") else []),
            "Cortes usados (momento | entra em | fonte da imagem | arquivo):", usados,
        ]) + "\n"
        video.with_suffix(".txt").write_text(ficha, encoding="utf-8")
        manifesto = {
            "pasta": pasta.name, "titulo": tema["titulo"], "titulos_ab": list(tema.get("titulos_ab") or []),
            "tema": tema["tema"], "descricao": descricao,
            "arquivo": video.name, "ficha": video.with_suffix(".txt").name, "miniatura": miniatura,
            "miniatura_limpa": limpa, "miniaturas_opcoes": opcoes, "texto_capa": tema.get("texto_capa", ""), "motivo": tema.get("motivo", ""),
            "duracao": round(total, 1), "fps": fps, "criado_em": time.time(), "capitulos": capitulos,
        }
        _gravar(pasta / MANIFESTO, manifesto)
        progresso.info(f"Pronto em {time.time() - t0:.0f}s -> {pasta}")
        return manifesto
    except BaseException:
        if pasta is not None and not (pasta / MANIFESTO).exists():  # montagem pela metade: não deixa pasta quebrada
            for sobra in pasta.iterdir():
                sobra.unlink(missing_ok=True)
            pasta.rmdir()
        raise
    finally:
        for sobra in trabalho.iterdir():  # só o que esta montagem criou
            sobra.unlink(missing_ok=True)
        trabalho.rmdir()


# ---------------------------------------------------------------- linha de comando

def _mostrar(dados: dict, saida: Path) -> None:
    por_id = {c["id"]: c for c in todos(saida)}
    quando = time.strftime("%d/%m %H:%M", time.localtime(dados.get("criado_em", 0)))
    print(f"\nTemas propostos em {quando}" + (f' para o pedido "{dados["pedido"]}"' if dados.get("pedido") else "") + ":")
    if not dados.get("atual", True):
        print("    (a biblioteca mudou depois desta proposta; use --de-novo para pedir temas com os cortes novos)")
    for n, tema in enumerate(dados["temas"], start=1):
        print(f"\n[{n}] {tema['titulo']}")
        for letra, outro in zip("BC", tema.get("titulos_ab") or []):
            print(f"    título {letra}: {outro}")
        print(f"    tema: {tema['tema']} | {len(tema['cortes'])} cortes | cerca de {_relogio(tema['duracao'], False)} min")
        if tema["motivo"]:
            print(f"    {tema['motivo']}")
        for k, i in enumerate(tema["cortes"], start=1):
            c = por_id.get(i)
            print(f"      {k:2d}. " + (f"{c['titulo']}  ({c['canal'] or 'sem canal'}, nota {c['nota']})" if c
                                        else f"(não está mais na biblioteca: {i})"))
    if not dados["temas"]:
        print("    A IA não encontrou nenhum tema com pelo menos "
              f"{MIN_CORTES} cortes. Junte mais cortes ou tente outro --pedido.")


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        fluxo.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(
        prog="cortes-compilar",
        description="Junta cortes da biblioteca numa compilação temática horizontal (1920x1080), para publicar como "
                    "vídeo normal do YouTube. Sem argumentos, mostra os temas propostos pela IA e pergunta qual gerar.")
    p.add_argument("tema", nargs="?", type=int, help="número do tema a gerar, da lista mostrada")
    p.add_argument("--pedido", default="", help='o tema que você quer, ex.: "bugs e bizarrices do mapa" (pede temas novos à IA)')
    p.add_argument("--de-novo", action="store_true", help="pede temas novos à IA em vez de mostrar a última proposta")
    p.add_argument("--listar", action="store_true", help="só mostra os temas, sem gerar nada")
    p.add_argument("--so-prontos", action="store_true",
                   help="não tenta baixar de novo os trechos originais: usa só os cortes verticais que já estão prontos")
    p.add_argument("--com-narrador", action="store_true",
                   help="mantém as falas do narrador dos cortes prontos (o padrão é tirar: entra só o vídeo de cada corte)")
    p.add_argument("--quadros", metavar="PASTA", default="",
                   help="não gera vídeo: grava uma folha de quadros por momento da compilação PASTA, para escolher a miniatura")
    p.add_argument("--miniatura", metavar="PASTA", default="",
                   help="não gera vídeo: grava uma miniatura da compilação PASTA com o quadro de --momento e --tempo")
    p.add_argument("--momento", type=int, default=1, help="com --miniatura: o número do momento (padrão: 1)")
    p.add_argument("--tempo", type=float, default=0.0, help="com --miniatura: o instante dentro do momento, em segundos")
    p.add_argument("--texto", default="", help="com --miniatura: até 3 palavras para escrever grande (padrão: sem texto)")
    p.add_argument("--nome", default="", help='com --miniatura: nome do arquivo (padrão: a próxima "miniatura opção N.jpg")')
    p.add_argument("--modelo", default=None, help="modelo do Claude para escolher os temas (padrão: o da sua conta)")
    p.add_argument("--marca", default=MARCA, help=f'assinatura do canal no canto do vídeo (padrão: {MARCA}; "" tira)')
    p.add_argument("--saida", type=Path, default=RAIZ / "saida", help="pasta dos cortes e das compilações (padrão: saida/)")
    a = p.parse_args()

    if a.quadros or a.miniatura:
        pasta = Path(a.quadros or a.miniatura)
        pasta = pasta if pasta.is_dir() else a.saida / pasta.name
        try:
            if a.quadros:
                for folha, titulo, quantos in folhas(pasta):
                    print(f"{folha}  |  {quantos} quadros, um a cada 2 s  |  {titulo}")
                print("\nO quadro k de cada folha (a contar de 0, linha por linha, 4 por linha) é o instante k x 2 s do momento.")
            else:
                print(miniatura(pasta, a.momento, a.tempo, a.texto, a.nome))
        except (RuntimeError, OSError, ValueError) as erro:
            sys.exit(f"ERRO: {erro}")
        return

    try:
        dados = None if a.pedido or a.de_novo else guardados(a.saida)
        if dados is None:
            if not ia.backend():
                sys.exit("ERRO: nenhum acesso ao Claude encontrado (defina ANTHROPIC_API_KEY ou instale/logue o Claude Code).")
            print(f"Pedindo temas à IA para {len(biblioteca(a.saida)[:LIMITE_LISTA])} cortes (uma chamada)...", flush=True)
            dados = propor(a.saida, a.pedido, a.modelo, a.marca)
        _mostrar(dados, a.saida)
        if a.listar or not dados["temas"]:
            return
        numero = a.tema
        if numero is None:
            if not sys.stdin.isatty():
                print("\nPara gerar um deles: cortes-compilar NÚMERO")
                return
            try:
                resposta = input("\nQual tema gerar? (número, ou Enter para sair) ").strip()
            except EOFError:  # entrada fechada (rodando por script): só mostra a lista
                return
            if not resposta:
                return
            numero = int(resposta) if resposta.isdigit() else 0
        if not 1 <= numero <= len(dados["temas"]):
            sys.exit(f"ERRO: escolha um tema de 1 a {len(dados['temas'])}.")
        print()
        manifesto = gerar(dados["temas"][numero - 1], a.saida, a.so_prontos, a.marca, a.com_narrador)
    except KeyboardInterrupt:
        print("\nInterrompido.")
        sys.exit(130)
    except (ia.IAErro, ValueError, RuntimeError, OSError) as erro:
        sys.exit(f"ERRO: {erro}")
    pasta = a.saida / manifesto["pasta"]
    print(f"\nVídeo:     {pasta / manifesto['arquivo']}\nMiniatura: {pasta / manifesto['miniatura']}"
          f"\nFicha:     {pasta / manifesto['ficha']}")


if __name__ == "__main__":
    main()
