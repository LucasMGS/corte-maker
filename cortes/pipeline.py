"""O pipeline completo: link ou arquivo -> cortes verticais + fichas + manifesto `cortes.json`."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import audio, baixar, editar, ffmpeg, gasto, ia, momentos, narrar, progresso, transcrever
from .config import config
from .momentos import Momento, hms

RAIZ = Path(__file__).resolve().parent.parent
MANIFESTO = "cortes.json"
FOLGA = 6.0  # segundos baixados a mais antes e depois de cada corte, porque o download corta em quadro-chave
MARCA = config("CORTES_MARCA")[:40]  # assinatura do canal de cortes no topo de cada vídeo (vazio: sem marca)
COMENTARIOS = ("pt", "en", "nao")  # idioma do comentário narrado, ou sem comentário


@dataclass
class Opcoes:
    cortes: int = 5
    dmin: int = 20
    dmax: int = 60
    layout: str = "blur"
    camera: str = ""  # enquadramento "camera": onde fica a câmera no vídeo, "x,y,largura,altura" em frações do quadro
    contexto: str = ""
    inicio: float | None = None
    fim: float | None = None
    idioma: str = "auto"
    whisper: str = "large-v3-turbo"
    modelo: str | None = None
    sem_ia: bool = False
    sem_legenda: bool = False
    marca: str = MARCA  # vazio: sem marca
    comentario: str = "pt"  # idioma do comentário que a IA escreve e uma voz narra em cada corte; "nao" desliga
    voz: str = ""  # voz do narrador, uma chave de `narrar.vozes()`; vazio usa a padrão
    abertura: bool = False  # o narrador também fala antes do corte (o padrão é só depois)
    # a IA sempre escreve o comentário, mas a voz (paga) só é gravada quando se pede, corte a corte, na interface;
    # com isto, todo corte já sai narrado
    narrar_na_hora: bool = False
    sem_traducao: bool = False  # legenda só no idioma falado, sem a segunda linha traduzida
    manter_pausas: bool = False  # não encurta os silêncios do corte (o padrão é tirar, para o ritmo não cair)
    baixar_tudo: bool = False  # links: baixa o vídeo inteiro em vez de só o áudio e os trechos dos cortes
    saida: Path = field(default_factory=lambda: RAIZ / "saida")
    # só no modo ao vivo
    intervalo: int = 150  # segundos de live nova entre uma análise e outra
    limiar: int = 75  # nota mínima para um momento virar corte
    triagem: bool = False  # um modelo barato lê cada trecho antes; a análise completa só roda quando ele vê chance de corte

    def validar(self) -> None:
        if not 1 <= self.cortes <= 30:
            raise ValueError("a quantidade de cortes deve ficar entre 1 e 30")
        if self.dmin < 5 or self.dmax <= self.dmin:
            raise ValueError("a duração mínima deve ser de pelo menos 5 s e menor que a máxima")
        if self.layout not in editar.LAYOUTS:
            raise ValueError(f"layout desconhecido: {self.layout}")
        if editar.ler_camera(self.camera) is None and self.layout == "camera":
            raise ValueError("para a câmera em cima, marque onde fica a câmera no vídeo")
        if len(self.marca) > 40:
            raise ValueError("a marca do canal deve ter no máximo 40 caracteres")
        if self.comentario not in COMENTARIOS:
            raise ValueError(f"comentário desconhecido: {self.comentario}")
        if self.voz and self.voz not in narrar.vozes():
            raise ValueError(f"voz desconhecida: {self.voz}")
        if self.inicio is not None and self.fim is not None and self.fim <= self.inicio:
            raise ValueError("o fim do trecho precisa vir depois do início")
        if not 60 <= self.intervalo <= 1800:
            raise ValueError("o intervalo entre análises deve ficar entre 60 e 1800 segundos")
        if not 0 <= self.limiar <= 100:
            raise ValueError("a nota mínima deve ficar entre 0 e 100")

    @property
    def narracao(self) -> str:
        """Idioma da narração, ou vazio quando ela está desligada ou a IA não vai escrever o texto."""
        return "" if self.comentario == "nao" or self.sem_ia else self.comentario


def segundos(texto: str) -> float:
    """Aceita 90, 1:30 ou 1:02:03."""
    total = 0.0
    for parte in texto.strip().split(":"):
        total = total * 60 + float(parte)
    return total


def nome_arquivo(texto: str, limite: int = 70) -> str:
    limpo = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", texto)
    limpo = re.sub(r"\s+", " ", limpo).strip(" .")
    return limpo[:limite].rstrip(" .") or "corte"


def _escolher(o: Opcoes, fonte: baixar.Fonte, linhas: list, energia, duracao: float) -> list[Momento]:
    # a narração só entra na chave quando existe: análises antigas, sem narração, continuam valendo.
    # O número é a versão do roteiro pedido à IA; sobe quando o formato da narração muda.
    chave = hashlib.sha1(json.dumps(
        [o.cortes, o.dmin, o.dmax, o.contexto, o.modelo, o.sem_ia, o.whisper, o.idioma]
        + ([o.narracao, o.abertura, 3] if o.narracao else [])).encode()).hexdigest()[:10]
    cache = fonte.pasta / f"momentos_{chave}.json"
    if cache.exists():
        progresso.info("Reaproveitando a análise anterior deste vídeo.")
        return [Momento(**m) for m in json.loads(cache.read_text(encoding="utf-8"))]

    achados: list[Momento] = []
    if o.sem_ia:
        progresso.info("IA desligada: usando picos de áudio.")
    elif len(linhas) < 5:
        progresso.info("Quase não há fala no vídeo: usando picos de áudio.")
    elif not ia.backend():
        progresso.aviso("Claude não encontrado (sem ANTHROPIC_API_KEY e sem Claude Code). Usando só picos de áudio.")
    else:
        try:
            achados = momentos.por_ia(linhas, fonte.titulo, fonte.canal, o.contexto, o.cortes, o.dmin, o.dmax, o.modelo,
                                      o.narracao, o.abertura)
            if not achados:
                progresso.info("A IA não achou momentos fortes: completando com picos de áudio.")
        except ia.IAErro as erro:
            dica = " Se for login expirado: abra um terminal, rode `claude` e digite /login." if ia.backend() == "cli" else ""
            progresso.aviso(f"IA indisponível ({erro}).{dica} Seguindo só com picos de áudio, com títulos genéricos.")

    usou_ia = bool(achados)
    if not achados:
        achados = momentos.por_audio(energia, linhas, fonte.titulo, o.cortes, o.dmin, o.dmax, duracao)
    if usou_ia or o.sem_ia:  # plano B por falha não vai para o cache: na próxima tenta a IA de novo
        cache.write_text(json.dumps([m.como_dict() for m in achados], ensure_ascii=False, indent=1), encoding="utf-8")
    return achados


def _hashtags(m: Momento) -> list[str]:
    return m.hashtags or ["shorts", "games", "cortes"]


def _ficha(m: Momento, fonte: baixar.Fonte, deslocamento: float, narrado: list[str]) -> str:
    hashtags = " ".join(f"#{t}" for t in _hashtags(m))
    credito = f"Créditos: {fonte.canal}" if fonte.canal else ""
    partes = [
        "TÍTULO", m.titulo, "",
        "DESCRIÇÃO / LEGENDA DO POST", "\n".join(x for x in (m.descricao, credito, hashtags) if x), "",
        "-" * 40,
    ]
    if m.gancho:
        partes += [f"Gancho na tela: {m.gancho}"]
    if narrado:
        partes += ["Narração: " + " [corte] ".join(narrado)]
    partes += [
        f"Trecho original: {hms(m.inicio + deslocamento)} a {hms(m.fim + deslocamento)} ({m.fim - m.inicio:.0f}s)",
        f"Nota: {m.nota}/100 ({ {'ia': 'IA', 'manual': 'corte manual'}.get(m.origem, 'pico de áudio') })",
    ]
    if m.motivo:
        partes.append(f"Por quê: {m.motivo}")
    if fonte.url:
        partes.append(f"Fonte: {fonte.url}")
    return "\n".join(partes) + "\n"


def pasta_nova(saida: Path, base: str) -> Path:
    """Cada rodada ganha a própria pasta: refazer o mesmo vídeo não sobrescreve nem esconde os cortes anteriores."""
    pasta, n = saida / base, 1
    while (pasta / MANIFESTO).exists():
        n += 1
        pasta = saida / f"{base} ({n})"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def publicar(m: Momento, numero: int, pasta: Path, fonte: baixar.Fonte, info: ffmpeg.Info, palavras: list[dict],
             o: Opcoes, deslocamento: float, video: Path | None = None, inicio_video: float = 0.0) -> dict:
    """Renderiza um corte com capa e ficha e devolve a entrada dele no manifesto.

    `m` e `palavras` estão no relógio da análise; `deslocamento` leva esse relógio para o do vídeo original.
    `video` é de onde sai a imagem (padrão: `fonte.video`) e `inicio_video` é o segundo da análise em que ele
    começa, para quando só o trecho do corte foi baixado.
    """
    destino = pasta / f"{numero:02d} - {nome_arquivo(m.titulo)}.mp4"
    if inicio_video:
        palavras = [{"i": p["i"] - inicio_video, "f": p["f"] - inicio_video, "t": p["t"]}
                    for p in palavras if m.inicio - 1 <= p["i"] <= m.fim + 1]
    ini, fim = m.inicio - inicio_video, m.fim - inicio_video
    falas = _narrar(m, o, pasta) if info.tem_audio else {}
    frases = _traduzir(_frases(palavras, ini, fim), falas, fonte.titulo, o)
    origem_video = video or fonte.video
    try:
        duracao = editar.renderizar(origem_video, info, ini, fim, palavras, m.titulo, destino, o.layout,
                                    not o.sem_legenda, o.marca.strip(), frases=frases, gancho=m.gancho,
                                    apertar=not o.manter_pausas, camera=o.camera, **falas)
        editavel = _guardar_receita(destino, origem_video, ini, fim, palavras, frases, m, o, fonte, falas)
    finally:
        for fala in falas.values():
            fala.arquivo.unlink(missing_ok=True)
    editar.capa(destino, destino.with_suffix(".jpg"))
    destino.with_suffix(".txt").write_text(_ficha(m, fonte, deslocamento, [f.texto for f in falas.values()]),
                                           encoding="utf-8")
    return {
        "arquivo": destino.name, "capa": destino.with_suffix(".jpg").name,
        "titulo": m.titulo, "descricao": m.descricao, "hashtags": _hashtags(m),
        "nota": m.nota, "origem": m.origem, "motivo": m.motivo, "gancho": m.gancho,
        "abertura": m.abertura if "abertura" in falas else "", "fecho": m.fecho if "fecho" in falas else "",
        "inicio": round(m.inicio + deslocamento, 2), "fim": round(m.fim + deslocamento, 2),
        "duracao": round(duracao, 1),
        # para refazer depois: enquadramento usado, se dá para refazer e o que o narrador diria
        "layout": o.layout, "camera": o.camera, "editavel": editavel, "narrado": bool(falas),
        "roteiro": {"abertura": m.abertura, "fecho": m.fecho} if (m.abertura or m.fecho) and info.tem_audio else {},
    }


FONTES = "_fontes"  # dentro da pasta da sessão: o trecho original e a receita de cada corte, para refazê-lo depois


def receita(corte: Path) -> tuple[Path, Path]:
    """Onde ficam o trecho original e a receita (JSON) de um corte."""
    pasta = corte.parent / FONTES
    return pasta / f"{corte.stem}.mp4", pasta / f"{corte.stem}.json"


def _guardar_receita(destino: Path, video: Path, ini: float, fim: float, palavras: list[dict], frases: list[dict],
                     m: Momento, o: Opcoes, fonte: baixar.Fonte, falas: dict[str, editar.Fala]) -> bool:
    """Guarda o trecho original do corte (sem nada por cima) e tudo o que a montagem usou, para o corte poder ser
    refeito com outro enquadramento ou com a narração. Falhar aqui não estraga o corte: só não dá para refazê-lo."""
    trecho, arquivo = receita(destino)
    try:
        trecho.parent.mkdir(exist_ok=True)
        ffmpeg.rodar(["-ss", f"{ini:.3f}", "-t", f"{fim - ini:.3f}", "-i", str(Path(video).resolve()),
                      "-map", "0:v:0", "-map", "0:a:0?", *editar.codec(), "-pix_fmt", "yuv420p",
                      "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", trecho.name], cwd=trecho.parent)
        gravadas = {}
        for nome, fala in falas.items():  # narração já gravada fica guardada: refazer não paga a voz de novo
            copia = trecho.parent / f"{destino.stem}.{nome}{fala.arquivo.suffix}"
            shutil.copyfile(fala.arquivo, copia)
            gravadas[nome] = {"arquivo": copia.name, "duracao": fala.duracao, "texto": fala.texto,
                              "traducao": fala.traducao, "marcas": fala.marcas, "voz": o.voz}
        perto = lambda p: ini - 0.05 <= p["i"] and p["f"] <= fim + 0.05
        dados = {
            "versao": 1, "titulo": m.titulo, "gancho": m.gancho, "marca": o.marca.strip(), "legenda": not o.sem_legenda,
            "apertar": not o.manter_pausas, "layout": o.layout, "camera": o.camera,
            "palavras": [{**p, "i": p["i"] - ini, "f": p["f"] - ini} for p in palavras if perto(p)],
            "frases": [{**f, "i": f["i"] - ini, "f": f["f"] - ini} for f in frases],
            "roteiro": {"abertura": m.abertura, "fecho": m.fecho}, "idioma": o.narracao or o.comentario,
            "traduzir": not (o.sem_legenda or o.sem_traducao or o.sem_ia), "assunto": fonte.titulo,
            "modelo": o.modelo, "voz": o.voz, "falas": gravadas, "narrado": bool(gravadas),
        }
        arquivo.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except Exception as erro:  # disco cheio, vídeo de origem estranho: o corte vale, só não poderá ser refeito
        progresso.aviso(f"Não deu para guardar o original de \"{m.titulo}\" para editar depois ({erro}).")
        trecho.unlink(missing_ok=True)
        arquivo.unlink(missing_ok=True)
        return False


_narracao_falhou = False  # avisa uma vez só; os cortes seguintes saem sem narração em vez de repetir o erro


def _narrar(m: Momento, o: Opcoes, pasta: Path) -> dict[str, editar.Fala]:
    """Grava a abertura e o fecho do corte. Sem narração possível, devolve vazio e o corte sai só com o original."""
    global _narracao_falhou
    falas: dict[str, editar.Fala] = {}
    if not o.narracao or not o.narrar_na_hora or _narracao_falhou:
        return falas
    try:
        for nome, texto in (("abertura", m.abertura if o.abertura else ""), ("fecho", m.fecho)):
            if texto:
                n = narrar.falar(texto, o.narracao, pasta / f"_{nome}", o.voz)
                falas[nome] = editar.Fala(n.arquivo, n.duracao, texto, marcas=n.palavras)
    except narrar.NarracaoErro as erro:
        _narracao_falhou = True
        progresso.aviso(f"Narração indisponível ({erro}). Os cortes saem sem o comentário narrado.")
        for fala in falas.values():
            fala.arquivo.unlink(missing_ok=True)
        return {}
    return falas


def _frases(palavras: list[dict], inicio: float, fim: float) -> list[dict]:
    """As falas do corte agrupadas em frases curtas: é nessa unidade que a legenda traduzida aparece."""
    frases: list[list[dict]] = []
    atual: list[dict] = []
    for p in palavras:
        if p["i"] < inicio - 0.05 or p["f"] > fim + 0.05:
            continue
        if atual:
            pausa = p["i"] - atual[-1]["f"]
            longa = p["f"] - atual[0]["i"] > 5 or len(atual) >= 12
            if pausa > 0.7 or longa or (atual[-1]["t"][-1] in ".?!…" and len(atual) >= 3):
                frases.append(atual)
                atual = []
        atual.append(p)
    if atual:
        frases.append(atual)
    return [{"i": f[0]["i"], "f": f[-1]["f"], "t": " ".join(p["t"] for p in f)} for f in frases]


_traducao_falhou = False  # mesmo critério da narração: avisa uma vez e segue sem


def _traduzir(frases: list[dict], falas: dict[str, editar.Fala], assunto: str, o: Opcoes) -> list[dict]:
    """Troca o texto de cada frase pela tradução e preenche a tradução das falas do narrador.
    Sem tradução possível, devolve vazio e o corte sai com a legenda em um idioma só."""
    global _traducao_falhou
    if o.sem_legenda or o.sem_traducao or o.sem_ia or _traducao_falhou or not ia.backend():
        return []
    narradas = list(falas.values())
    textos = [f.texto for f in narradas] + [f["t"] for f in frases]
    if not textos:
        return []
    try:
        traducoes = ia.traduzir(textos, assunto, o.modelo)
    except ia.IAErro as erro:
        _traducao_falhou = True
        progresso.aviso(f"Tradução da legenda indisponível ({erro}). Os cortes saem com a legenda em um idioma só.")
        return []
    for fala, traducao in zip(narradas, traducoes):
        fala.traducao = traducao
    return [{**f, "t": t} for f, t in zip(frases, traducoes[len(narradas):])]


TRAVA_MANIFESTO = threading.RLock()  # uma live grava o manifesto dela enquanto um corte da mesma pasta é refeito
EDITAVEIS = ("layout", "camera", "narrado", "abertura", "fecho", "roteiro", "duracao", "editado")  # o que `refazer` muda num corte


def gravar_manifesto(pasta: Path, manifesto: dict) -> None:
    with TRAVA_MANIFESTO:
        # uma live guarda o manifesto dela na memória: o que foi refeito depois, e já está no disco, prevalece
        try:
            no_disco = json.loads((pasta / MANIFESTO).read_text(encoding="utf-8"))
            refeitos = {c.get("arquivo"): c for c in no_disco.get("cortes", []) if c.get("editado")}
        except (OSError, ValueError, AttributeError):
            refeitos = {}
        for c in manifesto.get("cortes", []):
            r = refeitos.get(c.get("arquivo"))
            if r and r.get("editado", 0) > c.get("editado", 0):
                c.update({k: r[k] for k in EDITAVEIS if k in r})
        tmp = pasta / (MANIFESTO + ".tmp")  # troca atômica: a galeria lê este arquivo a cada segundo
        with tmp.open("w", encoding="utf-8") as arquivo:
            arquivo.write(json.dumps(manifesto, ensure_ascii=False, indent=1))
            arquivo.flush()
            os.fsync(arquivo.fileno())  # sem isto, uma queda de energia pode deixar o manifesto novo só com zeros
        tmp.replace(pasta / MANIFESTO)


def ler_manifesto(pasta: Path) -> dict | None:
    """O manifesto da pasta. Se ele estiver ilegível (queda de energia no meio da gravação), é refeito a partir
    das fichas dos cortes, que guardam quase tudo; só some o que não está nelas, como o enquadramento usado."""
    arquivo = pasta / MANIFESTO
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
        if isinstance(dados, dict):
            return dados
    except (OSError, ValueError):
        pass
    if not arquivo.exists():
        return None
    return reconstruir_manifesto(pasta)


def _ler_ficha(texto: str) -> dict:
    """Desfaz o que `_ficha` escreve."""
    blocos = texto.split("-" * 40, 1)
    topo, pe = blocos[0].splitlines(), (blocos[1] if len(blocos) > 1 else "").splitlines()
    item = {"titulo": "", "descricao": "", "hashtags": [], "canal": "", "motivo": "", "url": "", "abertura": "",
            "fecho": "", "nota": 0, "origem": "ia", "inicio": 0.0, "fim": 0.0}
    if "TÍTULO" in topo:
        item["titulo"] = next((l for l in topo[topo.index("TÍTULO") + 1:] if l.strip()), "").strip()
    if "DESCRIÇÃO / LEGENDA DO POST" in topo:
        descricao = []
        for linha in topo[topo.index("DESCRIÇÃO / LEGENDA DO POST") + 1:]:
            if linha.startswith("Créditos: "):
                item["canal"] = linha[len("Créditos: "):].strip()
            elif linha.startswith("#"):
                item["hashtags"] = [t.lstrip("#") for t in linha.split() if t.lstrip("#")]
            elif linha.strip():
                descricao.append(linha.strip())
        item["descricao"] = " ".join(descricao)
    for linha in pe:
        if linha.startswith("Narração: "):
            falas = linha[len("Narração: "):].split(" [corte] ")
            item["fecho"] = falas[-1].strip()
            item["abertura"] = falas[0].strip() if len(falas) > 1 else ""
        elif m := re.match(r"Trecho original: (\S+) a (\S+)", linha):
            item["inicio"], item["fim"] = segundos(m[1]), segundos(m[2])
        elif m := re.match(r"Nota: (\d+)/100 \((.*)\)", linha):
            item["nota"] = int(m[1])
            item["origem"] = {"IA": "ia", "corte manual": "manual"}.get(m[2], "audio")
        elif linha.startswith("Por quê: "):
            item["motivo"] = linha[len("Por quê: "):].strip()
        elif linha.startswith("Fonte: "):
            item["url"] = linha[len("Fonte: "):].strip()
    return item


def reconstruir_manifesto(pasta: Path) -> dict | None:
    """Refaz `cortes.json` a partir dos vídeos e das fichas que estão na pasta e grava o resultado."""
    itens, canal, url = [], "", ""
    for video in sorted(pasta.glob("*.mp4")):
        ficha = video.with_suffix(".txt")
        try:
            lido = _ler_ficha(ficha.read_text(encoding="utf-8"))
            duracao = ffmpeg.info(video).duracao
        except Exception:  # vídeo ou ficha cortados pela mesma queda: fica de fora
            continue
        canal, url = canal or lido.pop("canal"), url or lido.pop("url")
        lido.pop("canal", None), lido.pop("url", None)
        capa = video.with_suffix(".jpg")
        itens.append({"arquivo": video.name, "capa": capa.name if capa.exists() else "",
                      "titulo": lido.pop("titulo") or video.stem, **lido, "duracao": round(duracao, 1)})
    if not itens:
        return None
    ao_vivo = pasta.name.startswith("AO VIVO - ")
    manifesto = {
        "pasta": pasta.name, "titulo": pasta.name.removeprefix("AO VIVO - "), "canal": canal, "url": url,
        "criado_em": pasta.stat().st_ctime, "layout": "", "reconstruido": True,
        "janela_inicio": 0.0 if ao_vivo else min(c["inicio"] for c in itens),
        "janela_fim": max(c["fim"] for c in itens), "cortes": itens,
    }
    if ao_vivo:
        manifesto["ao_vivo"] = False  # marca a sessão como vinda de uma live, já encerrada
    gravar_manifesto(pasta, manifesto)
    return manifesto


def processar(origem: str, o: Opcoes) -> dict | None:
    """Roda tudo para uma origem. Devolve o manifesto gravado em `cortes.json`, ou None se nada foi encontrado."""
    o.validar()
    t0 = time.time()
    conta = gasto.abrir()
    trabalho = RAIZ / "trabalho"
    link = baixar.eh_link(origem)
    por_trechos = link and not o.baixar_tudo  # caminho econômico: áudio agora, vídeo só dos cortes
    dados: dict = {}
    info: ffmpeg.Info | None = None  # só existe quando o vídeo inteiro está em disco

    if link:
        progresso.etapa(1, 5, "Baixando só o áudio" if por_trechos else "Baixando o vídeo inteiro")
        dados = baixar.consultar(origem)
        progresso.titulo(dados.get("title") or origem)
        if dados.get("is_live"):
            raise RuntimeError("esta live ainda está no ar: ligue o modo ao vivo para acompanhá-la, "
                               "ou espere a gravação ficar disponível")
        fonte = (baixar.audio if por_trechos else baixar.inteiro)(dados, origem, trabalho, o.inicio, o.fim)
    else:
        progresso.etapa(1, 5, "Abrindo o vídeo")
        fonte = baixar.local(origem, trabalho)
    if not por_trechos:
        info = ffmpeg.info(fonte.video)
        if not info.tem_audio:
            raise RuntimeError("o vídeo não tem áudio; a análise depende dele")
    duracao = info.duracao if info else ffmpeg.duracao(fonte.video)
    progresso.titulo(fonte.titulo)
    progresso.info(f"{fonte.titulo} | {hms(duracao)}" + (f" | {info.largura}x{info.altura}" if info else ""))

    progresso.etapa(2, 5, "Analisando o áudio")
    wav = fonte.pasta / "audio.wav"
    audio.extrair_wav(fonte.video, wav)
    energia = audio.energia(wav)

    progresso.etapa(3, 5, "Transcrevendo")
    idioma = None if o.idioma == "auto" else o.idioma
    cache = fonte.pasta / f"transcricao_{nome_arquivo(o.whisper)}_{o.idioma}.json"
    if not cache.exists() and not transcrever.modelo_baixado(o.whisper):
        progresso.info("Baixando o modelo de transcrição (~1,6 GB, só na primeira vez)...")
    transcricao = transcrever.transcrever(wav, cache, o.whisper, idioma, duracao)
    palavras = transcricao["palavras"]
    linhas = momentos.montar_linhas(palavras, energia)
    progresso.info(f"{len(palavras)} palavras, idioma: {transcricao['idioma']}")

    progresso.etapa(4, 5, "Escolhendo os melhores momentos")
    escolhidos = _escolher(o, fonte, linhas, energia, duracao)
    if not escolhidos:
        progresso.info("Nenhum momento encontrado neste vídeo.")
        _fechar_conta(conta, "video", fonte.titulo, fonte.url, duracao, 0)
        _limpar(fonte, wav)
        return None

    progresso.etapa(5, 5, f"{'Baixando e editando' if por_trechos else 'Editando'} {len(escolhidos)} corte(s)")
    pasta = pasta_nova(o.saida, nome_arquivo(fonte.titulo, 60))
    deslocamento = (o.inicio or 0.0) if link else 0.0
    itens = []
    for i, m in enumerate(escolhidos, start=1):
        m.fim = min(m.fim, duracao)
        if por_trechos:
            # baixa o trecho com folga e descobre pelo áudio onde ele realmente começa
            pedido = max(0.0, m.inicio - FOLGA)
            bruto = baixar.trecho(dados, deslocamento + pedido, deslocamento + m.fim + FOLGA, fonte.pasta / f"trecho_{i:02d}")
            try:
                comeco = audio.alinhar(wav, bruto, pedido)
                info_trecho = ffmpeg.info(bruto)
                m.inicio = max(m.inicio, comeco)
                m.fim = min(m.fim, comeco + info_trecho.duracao)
                itens.append(publicar(m, i, pasta, fonte, info_trecho, palavras, o, deslocamento, bruto, comeco))
            finally:
                bruto.unlink(missing_ok=True)
        else:
            itens.append(publicar(m, i, pasta, fonte, info, palavras, o, deslocamento))
        progresso.pct(100 * i / len(escolhidos))
        progresso.info(f"{i:02d} | nota {m.nota:3d} | {m.fim - m.inicio:4.0f}s | {m.titulo}")

    manifesto = {
        "pasta": pasta.name, "titulo": fonte.titulo, "canal": fonte.canal, "url": fonte.url,
        "criado_em": time.time(), "layout": o.layout,
        # janela do vídeo original que foi analisada, para desenhar a linha do tempo
        "janela_inicio": deslocamento, "janela_fim": deslocamento + duracao,
        "cortes": itens,
        "gasto": _fechar_conta(conta, "video", fonte.titulo, fonte.url, duracao, len(itens)),
    }
    gravar_manifesto(pasta, manifesto)
    _limpar(fonte, wav)
    progresso.info(f"Pronto em {time.time() - t0:.0f}s -> {pasta}")
    return manifesto


def _fechar_conta(conta: gasto.Conta, tipo: str, titulo: str, url: str, segundos: float, cortes: int) -> dict:
    """Mostra o gasto do trabalho, anota na planilha `gastos.csv` e devolve o resumo para o manifesto."""
    progresso.info(conta.frase())
    progresso.dado("gasto", conta.resumo())
    gasto.registrar(conta, tipo, titulo, url, segundos, cortes)
    return conta.resumo()


def _limpar(fonte: baixar.Fonte, wav: Path) -> None:
    """Depois de terminar, apaga o que é pesado e dá para obter de novo: o áudio extraído e o que foi baixado ou
    copiado para `trabalho/`. Ficam só a transcrição e a análise (alguns KB), que aceleram uma nova rodada.
    O arquivo original de um vídeo local nunca é tocado."""
    wav.unlink(missing_ok=True)
    if fonte.video.resolve().is_relative_to((RAIZ / "trabalho").resolve()):
        fonte.video.unlink(missing_ok=True)
