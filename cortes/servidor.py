"""Interface web local: fila de trabalhos, progresso e galeria dos cortes.

Só biblioteca padrão (http.server) e escuta apenas em 127.0.0.1.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import html
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import aovivo, baixar, compilar, ffmpeg, ia, narrar, pipeline, progresso, publicar, refazer, tiktok
from .config import config

RAIZ = pipeline.RAIZ
SAIDA = RAIZ / "saida"
ENVIOS = RAIZ / "trabalho" / "envios"
PAGINA = Path(__file__).parent / "web" / "index.html"
CONEXAO_CAIU = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def _pagina_preenchida() -> str:
    """Troca os marcadores `{{...}}` da página pelo que está configurado no ambiente ou no `.env`."""
    site = config("CORTES_SITE_URL").rstrip("/")
    if site:
        s = html.escape(site)
        paginas = ("Nos campos obrigatórios, use as páginas do seu app:"
                   f"<br>Terms of Service: <code>{s}/termos.html</code>"
                   f"<br>Privacy Policy: <code>{s}/privacidade.html</code>"
                   f"<br>Web/Desktop URL: <code>{s}/</code>")
    else:
        paginas = ("Os campos obrigatórios pedem o endereço dos Termos de uso, da Política de privacidade e do site "
                   "do app: publique páginas suas (o GitHub Pages serve, de graça) e, se quiser vê-las aqui, ponha o "
                   "endereço em <code>CORTES_SITE_URL</code> no <code>.env</code>.")
    return (PAGINA.read_text(encoding="utf-8")
            .replace("{{CORTES_MARCA}}", html.escape(pipeline.MARCA, quote=True))
            .replace("{{CORTES_TIKTOK_PAGINAS}}", paginas))


# ---------------------------------------------------------------- fila de trabalhos

@dataclass
class Trabalho:
    id: str
    origem: str
    opcoes: pipeline.Opcoes
    estado: str = "fila"  # fila | rodando | pronto | vazio | erro
    etapa: int = 0
    etapa_texto: str = "Na fila"
    pct: float | None = None
    titulo: str = ""
    registro: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    erro: str = ""
    pasta: str = ""
    n_cortes: int = 0
    criado: float = field(default_factory=time.time)
    ao_vivo: bool = False
    vivo: dict = field(default_factory=dict)  # modo ao vivo: fase, decorrido, proxima, n_cortes, ultimo, pasta
    parar: threading.Event = field(default_factory=threading.Event)
    manuais: queue.Queue = field(default_factory=queue.Queue)  # cortes pedidos à mão numa live: (fim, duração)
    compilacao: dict | None = None  # o tema a montar, quando o trabalho é uma compilação e não um vídeo a cortar

    def publico(self) -> dict:
        return {k: getattr(self, k) for k in (
            "id", "origem", "estado", "etapa", "etapa_texto", "pct", "titulo", "avisos", "erro", "pasta", "n_cortes",
            "ao_vivo", "vivo",
        )} | {"registro": self.registro[-60:], "parando": self.parar.is_set(), "contexto": self.opcoes.contexto,
              "tipo": "compilacao" if self.compilacao is not None else "video",
              "limiar": self.opcoes.limiar,
              # todas as opções do trabalho, para a página mostrar e ajustar as que podem mudar no meio
              "opcoes": {k: v for k, v in dataclasses.asdict(self.opcoes).items() if k != "saida"}}


class RelatorWeb(progresso.Relator):
    def __init__(self, t: Trabalho) -> None:
        self.t = t

    def etapa(self, n: int, total: int, texto: str) -> None:
        self.t.etapa, self.t.etapa_texto, self.t.pct = n, texto, None
        self.t.registro.append(f"[{n}/{total}] {texto}")

    def info(self, texto: str) -> None:
        self.t.registro.append(texto)
        del self.t.registro[:-300]

    def aviso(self, texto: str) -> None:
        self.t.avisos.append(texto)
        self.info(texto)

    def pct(self, valor: float) -> None:
        self.t.pct = round(valor, 1)

    def titulo(self, texto: str) -> None:
        self.t.titulo = texto

    def dado(self, chave: str, valor) -> None:
        self.t.vivo = self.t.vivo | {chave: valor}  # troca o dicionário inteiro: outra thread pode estar lendo


trabalhos: dict[str, Trabalho] = {}
fila: queue.Queue[str] = queue.Queue()

# As lives sendo acompanhadas ficam anotadas em disco, com as opções de cada uma: se o programa fechar sem a pessoa
# pedir (janela fechada, computador reiniciado, processo derrubado), elas voltam sozinhas quando ele abrir de novo.
# Um arquivo por porta: a cópia de teste (outra porta) não retoma as lives da interface de verdade.
_lives_arquivo: Path | None = None
_trava_lives = threading.Lock()


def _guardar_lives() -> None:
    if _lives_arquivo is None:
        return
    with _trava_lives:
        lista = [{"origem": t.origem, "desde": t.criado,
                  "opcoes": {k: v for k, v in dataclasses.asdict(t.opcoes).items() if k != "saida"}}
                 for t in list(trabalhos.values())
                 if t.ao_vivo and t.estado in ("fila", "rodando") and not t.parar.is_set()]
        try:
            _lives_arquivo.parent.mkdir(parents=True, exist_ok=True)
            tmp = _lives_arquivo.with_name(_lives_arquivo.name + ".tmp")
            tmp.write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(_lives_arquivo)
        except OSError:
            pass  # sem anotar, a live segue normal; só não volta sozinha se o programa cair


def _retomar_lives() -> int:
    """Recoloca as lives que estavam sendo acompanhadas quando o programa fechou. Devolve quantas voltaram."""
    try:
        lista = json.loads(_lives_arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError, AttributeError):
        return 0
    campos = {f.name for f in dataclasses.fields(pipeline.Opcoes)}
    voltaram = 0
    for r in lista if isinstance(lista, list) else []:
        try:
            opcoes = pipeline.Opcoes(**{k: v for k, v in r["opcoes"].items() if k in campos})
            opcoes.validar()
            origem = str(r["origem"])
        except (KeyError, TypeError, ValueError):
            continue  # anotação de uma versão antiga ou estragada: essa não volta
        if any(t.ao_vivo and t.origem == origem for t in trabalhos.values()):
            continue
        t = Trabalho(uuid.uuid4().hex[:10], origem, opcoes, ao_vivo=True)
        t.registro.append("Retomada sozinha: o programa tinha fechado enquanto esta live era acompanhada.")
        t.estado = "rodando"
        trabalhos[t.id] = t
        threading.Thread(target=_rodar, args=(t,), daemon=True).start()
        voltaram += 1
    _guardar_lives()
    return voltaram


def _rodar(t: Trabalho) -> None:
    t.estado = "rodando"
    progresso.usar(RelatorWeb(t))  # vale só para esta thread
    try:
        if t.compilacao is not None:
            manifesto = compilar.gerar(t.compilacao, SAIDA, marca=t.opcoes.marca.strip())
        elif t.ao_vivo:
            manifesto = aovivo.monitorar(t.origem, t.opcoes, t.parar, t.manuais)
        else:
            manifesto = pipeline.processar(t.origem, t.opcoes)
        if manifesto:
            t.estado, t.pasta = "pronto", manifesto["pasta"]
            t.n_cortes = len(manifesto["capitulos" if t.compilacao is not None else "cortes"])
        else:
            t.estado = "vazio"
        t.etapa_texto = "Concluído"
    except Exception as erro:  # a thread não pode morrer por causa de um vídeo
        t.estado, t.erro = "erro", str(erro).strip() or type(erro).__name__
    finally:
        t.pct = None
        progresso.usar(progresso.Relator())
        if t.ao_vivo:
            _guardar_lives()


def _operario() -> None:
    """Vídeos gravados e compilações: um por vez, em fila. As lives não passam por aqui: cada uma roda na própria thread, sem
    limite de quantidade (todas disputam a mesma placa de vídeo e a mesma cota da IA)."""
    while True:
        t = trabalhos.get(fila.get())
        if t is None:  # removido enquanto esperava
            continue
        _rodar(t)


# ---------------------------------------------------------------- regras das rotas

class Recusa(Exception):
    def __init__(self, status: int, detalhe: str) -> None:
        self.status, self.detalhe = status, detalhe


def _biblioteca() -> list[dict]:
    sessoes = []
    if SAIDA.is_dir():
        for arquivo in SAIDA.glob(f"*/{pipeline.MANIFESTO}"):
            dados = pipeline.ler_manifesto(arquivo.parent)  # refaz pelas fichas se o arquivo estiver ilegível
            if dados is None:
                continue
            dados["pasta"] = arquivo.parent.name
            dados["cortes"] = [c for c in dados.get("cortes", []) if (arquivo.parent / c.get("arquivo", "")).is_file()]
            if dados["cortes"]:
                sessoes.append(dados)
    return sorted(sessoes, key=lambda s: s.get("criado_em", 0), reverse=True)


def _estado() -> dict:
    return {
        "trabalhos": [t.publico() for t in sorted(trabalhos.values(), key=lambda t: t.criado, reverse=True)],
        "biblioteca": _biblioteca(),
        "compilacoes": _compilacoes(),
        "youtube": publicar.estado(),
        "tiktok": tiktok.estado(),
        "sistema": {"ia": ia.backend(), "ia_falha": ia.ultima_falha, "gpu": ffmpeg.tem_nvenc()},
    }


# ---------------------------------------------------------------- compilações

_proposta = {"rodando": False, "erro": ""}  # o pedido de temas à IA roda fora da fila: não usa a placa de vídeo


def _compilacoes() -> dict:
    """O que a página mostra sobre compilações: os temas da última proposta, com os cortes de cada um, e as prontas."""
    cortes = compilar.todos(SAIDA)
    por_id = {c["id"]: c for c in cortes}
    dados = compilar.guardados(SAIDA, cortes) or {}
    temas = []
    for tema in dados.get("temas", []):
        achados = [por_id[i] for i in tema["cortes"] if i in por_id]
        temas.append({k: tema[k] for k in ("tema", "titulo", "descricao", "motivo", "texto_capa", "duracao")} | {
            "cortes": [{k: c[k] for k in ("pasta", "capa", "titulo", "canal", "nota")} for c in achados],
            "faltam": len(tema["cortes"]) - len(achados)})
    return {"temas": temas, "proposto_em": dados.get("criado_em"), "pedido": dados.get("pedido", ""),
            "atual": dados.get("atual", True), "tem_proposta": bool(dados), "minimo": compilar.MIN_CORTES,
            "propondo": _proposta["rodando"], "erro": _proposta["erro"], "prontas": compilar.prontas(SAIDA)}


def _propor_temas(d: dict) -> dict:
    """Pede à IA os temas de compilação (uma chamada). A resposta aparece em `/api/estado` quando chegar."""
    if not ia.backend():
        raise Recusa(400, "Sem acesso ao Claude: os temas de compilação são escolhidos pela IA.")
    if _proposta["rodando"]:
        raise Recusa(409, "Os temas já estão sendo escolhidos.")
    pedido = str(d.get("pedido", "")).strip()
    marca = str(d.get("marca", pipeline.MARCA)).strip()[:40]
    _proposta.update(rodando=True, erro="")

    def alvo() -> None:
        try:
            compilar.propor(SAIDA, pedido, marca=marca)
        except Exception as erro:  # sem IA, biblioteca pequena demais, resposta fora do formato
            _proposta["erro"] = str(erro).strip() or type(erro).__name__
        finally:
            _proposta["rodando"] = False

    threading.Thread(target=alvo, daemon=True).start()
    return {"ok": True}


def _compilar(d: dict) -> dict:
    """Põe na fila a montagem de um dos temas propostos."""
    dados = compilar.guardados(SAIDA) or {}
    try:
        tema = dados["temas"][int(d.get("tema"))]
    except (KeyError, IndexError, TypeError, ValueError):
        raise Recusa(404, "Tema não encontrado. Peça os temas de novo.")
    if d.get("titulo") not in (None, tema["titulo"]):  # a lista mudou entre a página ser desenhada e o clique
        raise Recusa(409, "A lista de temas mudou. Confira e clique de novo.")
    if any(t.compilacao is not None and t.origem == tema["titulo"] and t.estado in ("fila", "rodando")
           for t in trabalhos.values()):
        raise Recusa(409, "Esta compilação já está na fila.")
    marca = str(d.get("marca", pipeline.MARCA)).strip()[:40]
    t = Trabalho(uuid.uuid4().hex[:10], tema["titulo"], pipeline.Opcoes(marca=marca), titulo=tema["titulo"],
                 compilacao=tema)
    trabalhos[t.id] = t
    fila.put(t.id)
    return {"criados": [t.id]}


def _vozes() -> dict:
    """As vozes do narrador para o seletor da página. As da ElevenLabs só aparecem como usáveis se houver chave."""
    tem = narrar.tem_eleven()
    return {
        "padrao": narrar.padrao(),
        "vozes": [{"id": id, "nome": v.nome, "jeito": v.jeito, "motor": v.motor, "usavel": tem or v.motor != "elevenlabs"}
                  for id, v in narrar.vozes().items()],
    }


def _amostra(id: str) -> Path:
    try:
        return narrar.amostra(id)
    except narrar.NarracaoErro as erro:
        raise Recusa(404 if "desconhecida" in str(erro) else 502, f"Não foi possível gerar a amostra: {erro}.")


def _tempo(valor) -> float | None:
    if valor in (None, ""):
        return None
    try:
        return pipeline.segundos(str(valor))
    except ValueError:
        raise Recusa(400, f"Tempo inválido: {valor!r}. Use 90, 1:30 ou 1:02:03.")


def _criar(d: dict) -> dict:
    origens = [linha.strip().strip('"') for linha in str(d.get("origem", "")).splitlines() if linha.strip()]
    if not origens:
        raise Recusa(400, "Cole um link ou escolha um arquivo de vídeo.")
    ao_vivo = bool(d.get("ao_vivo"))
    for origem in origens:
        if ao_vivo and not baixar.eh_link(origem):
            raise Recusa(400, "O modo ao vivo precisa do link da live.")
        if not baixar.eh_link(origem) and not Path(origem).expanduser().is_file():
            raise Recusa(400, f"Não é um link nem um arquivo existente: {origem}")
    try:
        opcoes = pipeline.Opcoes(
            cortes=int(d.get("cortes", 5)), dmin=int(d.get("dmin", 20)), dmax=int(d.get("dmax", 60)),
            layout=str(d.get("layout", "blur")), camera=str(d.get("camera", "")).strip(), contexto=str(d.get("contexto", "")).strip()[:500],
            inicio=_tempo(d.get("inicio")), fim=_tempo(d.get("fim")), idioma=str(d.get("idioma", "auto")),
            sem_ia=not d.get("usar_ia", True), sem_legenda=not d.get("legendas", True),
            marca=str(d.get("marca", pipeline.MARCA)).strip(), comentario=str(d.get("comentario", "pt")),
            voz=str(d.get("voz", "")),
            abertura=bool(d.get("abertura", False)), sem_traducao=not d.get("traducao", True),
            limiar=int(d.get("limiar", 75)), intervalo=int(d.get("intervalo", 150)),
            triagem=bool(d.get("triagem", False)),
        )
        opcoes.validar()
    except (TypeError, ValueError) as erro:
        raise Recusa(400, str(erro).capitalize() + ".")

    criados = []
    for origem in origens:
        # cada trabalho tem a sua cópia das opções: o que procurar pode ser trocado numa live sem mexer nas outras
        t = Trabalho(uuid.uuid4().hex[:10], origem, dataclasses.replace(opcoes), ao_vivo=ao_vivo)
        trabalhos[t.id] = t
        if ao_vivo:  # começa já, em paralelo com as outras lives e com a fila de vídeos gravados
            t.estado = "rodando"
            threading.Thread(target=_rodar, args=(t,), daemon=True).start()
        else:
            fila.put(t.id)
        criados.append(t.id)
    if ao_vivo:
        _guardar_lives()
    return {"criados": criados}


def _remover(id: str) -> dict:
    t = trabalhos.get(id)
    if t is None:
        raise Recusa(404, "Trabalho não encontrado.")
    if t.estado == "rodando":
        raise Recusa(409, "Pare o monitoramento antes de dispensar." if t.ao_vivo
                     else "Este vídeo já está sendo processado e não pode ser cancelado.")
    del trabalhos[id]
    return {"ok": True}


def _parar(id: str) -> dict:
    """Pede para um monitoramento ao vivo encerrar: ele fecha a gravação, analisa o resto e termina sozinho."""
    t = trabalhos.get(id)
    if t is None or not t.ao_vivo:
        raise Recusa(404, "Monitoramento não encontrado.")
    t.parar.set()
    if t.estado == "fila":
        del trabalhos[id]
    _guardar_lives()  # parada a pedido: não volta mais sozinha
    return {"ok": True}


# o que pode mudar num trabalho que ainda vai analisar: campo do pedido -> (campo das opções, conversão, nome no registro)
AJUSTES = {
    "contexto": ("contexto", lambda v: str(v).strip()[:500], "O que procurar"),
    "limiar": ("limiar", int, "Nota mínima"),
    "layout": ("layout", str, "Enquadramento"),
    "camera": ("camera", lambda v: str(v).strip(), "Posição da câmera"),
    "dmin": ("dmin", int, "Duração mínima"),
    "dmax": ("dmax", int, "Duração máxima"),
    "voz": ("voz", str, "Voz do narrador"),
    "comentario": ("comentario", str, "Comentário narrado"),
    "marca": ("marca", lambda v: str(v).strip(), "Marca do canal"),
    "abertura": ("abertura", bool, "Narrador também no início"),
    "traducao": ("sem_traducao", lambda v: not v, "Legenda só em um idioma"),
    "legendas": ("sem_legenda", lambda v: not v, "Sem legenda"),
}


def _contexto(id: str, d: dict) -> dict:
    """Ajusta um trabalho que ainda vai analisar (uma live no ar ou um vídeo na fila). Numa live, o que procurar e
    a nota mínima valem a partir da próxima análise; enquadramento, voz e legenda, a partir do próximo corte."""
    t = trabalhos.get(id)
    if t is None:
        raise Recusa(404, "Trabalho não encontrado.")
    if not (t.estado == "fila" or (t.ao_vivo and t.estado == "rodando")):
        raise Recusa(409, "Só dá para ajustar uma live no ar ou um vídeo que ainda está na fila.")
    mudancas: dict = {}
    nomes = {destino: nome for destino, _, nome in AJUSTES.values()}
    for campo, valor in d.items():
        if campo in AJUSTES:
            destino, converter, nome = AJUSTES[campo]
            try:
                mudancas[destino] = converter(valor)
            except (TypeError, ValueError):
                raise Recusa(400, f"Valor inválido para {nome.lower()}: {valor!r}.")
    try:
        dataclasses.replace(t.opcoes, **mudancas).validar()  # confere o conjunto antes de mexer no trabalho
    except ValueError as erro:
        raise Recusa(400, str(erro).capitalize() + ".")
    for destino, valor in mudancas.items():
        if getattr(t.opcoes, destino) != valor:
            setattr(t.opcoes, destino, valor)  # o mesmo objeto que a live em andamento consulta a cada análise
            t.registro.append(f"{nomes[destino]} agora: {valor if valor != '' else '(vazio)'}")
    if t.ao_vivo:
        _guardar_lives()  # a live volta com o ajuste se o programa reabrir
    return {"ok": True, "contexto": t.opcoes.contexto, "limiar": t.opcoes.limiar, "layout": t.opcoes.layout,
            "camera": t.opcoes.camera}


def _vivo(id: str) -> Trabalho:
    t = trabalhos.get(id)
    if t is None or not t.ao_vivo or t.estado != "rodando":
        raise Recusa(404, "Esta live não está sendo monitorada agora.")
    return t


def _cortar(id: str, d: dict) -> dict:
    """Pede um corte feito à mão numa live no ar: os últimos `duracao` segundos até `fim` (segundo da gravação
    que a pessoa estava vendo na prévia; sem ele, até o que já foi gravado)."""
    t = _vivo(id)
    try:
        duracao = int(d.get("duracao", 45))
        fim = None if d.get("fim") is None else float(d["fim"])
    except (TypeError, ValueError):
        raise Recusa(400, "Pedido de corte inválido.")
    if not 10 <= duracao <= 120:
        raise Recusa(400, "O corte manual deve ter de 10 a 120 segundos.")
    if t.parar.is_set():
        raise Recusa(409, "Esta live já está sendo encerrada.")
    t.manuais.put((fim, duracao))
    t.registro.append(f"Corte manual pedido: últimos {duracao} s" + (f" até {fim:.0f} s da gravação." if fim is not None else "."))
    return {"ok": True}


def _previa(id: str, arq: str) -> Path:
    """Um pedaço de ~20 s da gravação da live, convertido para MP4 sem recodificar, para tocar no navegador."""
    t = _vivo(id)
    pasta = RAIZ / "trabalho" / str(t.vivo.get("trabalho", ""))
    if not re.fullmatch(r"p_\d{6}", arq) or not re.fullmatch(r"aovivo_\d+_[0-9a-f]{6}", pasta.name):
        raise Recusa(404, "Prévia não encontrada.")
    ts, mp4 = pasta / f"{arq}.ts", pasta / f"previa_{arq}.mp4"
    if not mp4.exists():
        if not ts.exists():
            raise Recusa(404, "Esse trecho já saiu do disco.")
        tmp = pasta / f"previa_{arq}_{uuid.uuid4().hex[:6]}.tmp.mp4"
        try:
            ffmpeg.rodar(["-i", str(ts), "-c", "copy", "-movflags", "+faststart", str(tmp)])
            tmp.replace(mp4)
        except (RuntimeError, OSError):
            tmp.unlink(missing_ok=True)
            raise Recusa(404, "Não foi possível preparar a prévia deste trecho.")
        for velho in sorted(pasta.glob("previa_p_*.mp4"))[:-3]:  # só as últimas ficam em disco
            velho.unlink(missing_ok=True)
    return mp4


def _foto(entrada: list[str]) -> dict:
    """Um quadro do vídeo em JPEG (base64), para marcar onde fica a câmera."""
    try:
        jpg = ffmpeg.saida([*entrada, "-frames:v", "1", "-vf", "scale='min(960,iw)':-2", "-q:v", "4",
                            "-f", "image2pipe", "-c:v", "mjpeg", "-"])
    except (RuntimeError, OSError) as erro:
        raise Recusa(502, f"Não foi possível pegar uma imagem do vídeo: {erro}")
    if not jpg:
        raise Recusa(502, "Não foi possível pegar uma imagem do vídeo.")
    return {"imagem": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}


def _quadro(d: dict) -> dict:
    """Um quadro do vídeo que vai ser cortado (o primeiro da lista), sem baixar o vídeo."""
    origem = next((linha.strip().strip('"') for linha in str(d.get("origem", "")).splitlines() if linha.strip()), "")
    if not origem:
        raise Recusa(400, "Cole um link ou escolha um arquivo de vídeo primeiro.")
    if not baixar.eh_link(origem):
        video = Path(origem).expanduser()
        if not video.is_file():
            raise Recusa(400, f"Não é um link nem um arquivo existente: {origem}")
        meio = min(60.0, ffmpeg.duracao(video) * 0.3)
        return _foto(["-ss", f"{meio:.1f}", "-i", str(video)])
    try:
        dados = baixar.consultar(origem)
    except Exception as erro:
        raise Recusa(502, f"Não foi possível ler o link: {erro}")
    formatos = [f for f in dados.get("formats") or [dados]
                if f.get("url") and f.get("vcodec") not in (None, "none") and (f.get("height") or 0) <= 1080]
    if not formatos:
        raise Recusa(502, "O link não tem uma versão com imagem que dê para ler.")
    f = max(formatos, key=lambda f: ((f.get("height") or 0) <= 720, f.get("height") or 0))  # até 720p basta
    cabecalhos = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
    entrada = ["-headers", cabecalhos] if cabecalhos else []
    if not dados.get("is_live"):
        entrada += ["-ss", f"{min(60.0, (dados.get('duration') or 0) * 0.3):.1f}"]
    return _foto([*entrada, "-i", f["url"]])


def _quadro_vivo(id: str) -> dict:
    """Um quadro do trecho mais recente já gravado de uma live em andamento."""
    t = _vivo(id)
    pasta = RAIZ / "trabalho" / str(t.vivo.get("trabalho", ""))
    pedacos = sorted(pasta.glob("p_*.ts")) if re.fullmatch(r"aovivo_\d+_[0-9a-f]{6}", pasta.name) else []
    if not pedacos:
        raise Recusa(404, "A live ainda não gravou nenhum trecho.")
    return _foto(["-i", str(pedacos[-2] if len(pedacos) > 1 else pedacos[-1])])  # o último pode estar sendo escrito


def _corte(d: dict) -> tuple[Path, str]:
    video = _dentro_da_saida(str(d.get("pasta", "")), str(d.get("arquivo", "")))
    if video.suffix.lower() != ".mp4" or video.parent.parent != SAIDA.resolve():
        raise Recusa(404, "Corte não encontrado.")
    return video.parent, video.name


def _quadro_corte(d: dict) -> dict:
    """Um quadro do trecho original guardado de um corte, para marcar a câmera ao refazê-lo."""
    try:
        video = refazer.trecho(*_corte(d))
    except refazer.RefazerErro as erro:
        raise Recusa(409, str(erro).capitalize() + ".")
    return _foto(["-ss", f"{ffmpeg.duracao(video) / 2:.1f}", "-i", str(video)])


def _refazer(d: dict) -> dict:
    pasta, arquivo = _corte(d)
    narrado = d.get("narrado")
    try:
        item = refazer.refazer(pasta, arquivo, layout=d.get("layout") or None,
                               camera=None if d.get("camera") is None else str(d["camera"]),
                               narrado=None if narrado is None else bool(narrado),
                               voz=None if d.get("voz") is None else str(d["voz"]),
                               textos=d.get("textos") if isinstance(d.get("textos"), dict) else None)
    except refazer.RefazerErro as erro:
        raise Recusa(409, str(erro).capitalize() + ".")
    return {"ok": True, "corte": item}


def _cancelar_refazer(d: dict) -> dict:
    if not refazer.cancelar(*_corte(d)):
        raise Recusa(409, "Este corte não está sendo refeito agora.")
    return {"ok": True}


def _dentro_da_saida(*partes: str) -> Path:
    caminho = SAIDA.joinpath(*partes).resolve()
    if not partes or not all(partes) or not caminho.is_relative_to(SAIDA.resolve()) or not caminho.exists():
        raise Recusa(404, "Arquivo não encontrado.")
    return caminho


def _abrir(d: dict) -> dict:
    """Abre a pasta dos cortes no explorador de arquivos."""
    pasta = _dentro_da_saida(str(d.get("pasta", "")))
    if sys.platform == "win32":
        os.startfile(pasta)  # caminho validado acima, sempre dentro de saida/
    else:
        webbrowser.open(pasta.as_uri())
    return {"ok": True}


# ---------------------------------------------------------------- publicar no YouTube

def _youtube(acao, *argumentos):
    """Chama o módulo de publicação e devolve os erros dele como recusa, com a frase que ele já escreveu."""
    try:
        return acao(*argumentos)
    except publicar.Erro as erro:
        raise Recusa(400, str(erro))


RETORNO_LOGIN = """<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Corte Maker</title>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#0f0a15;color:#f4eef8;font:16px/1.5 system-ui,sans-serif">
<div style="max-width:440px;padding:24px;text-align:center"><h1 style="font-size:22px;margin:0 0 10px">{titulo}</h1><p style="color:#ab9cba;margin:0">{texto}</p></div>
<script>setTimeout(() => window.close(), 2500)</script></body></html>"""


def _retorno_login(parametros: dict) -> str:
    """A página que o Google abre depois da autorização: conclui o login e diz o que aconteceu."""
    limpar = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;")
    try:
        canal = publicar.concluir_login(parametros)
        return RETORNO_LOGIN.format(titulo="Canal conectado", texto=limpar(
            (f"O corte-maker agora publica em {canal}. " if canal else "O corte-maker já pode publicar no seu canal. ")
            + "Pode fechar esta aba e voltar para a interface."))
    except publicar.Erro as erro:
        return RETORNO_LOGIN.format(titulo="Não deu para conectar", texto=limpar(erro))


def _tiktok(acao, *argumentos):
    try:
        return acao(*argumentos)
    except tiktok.Erro as erro:
        raise Recusa(400, str(erro))


def _retorno_tiktok(parametros: dict) -> str:
    limpar = lambda t: str(t).replace("&", "&amp;").replace("<", "&lt;")
    try:
        conta = tiktok.concluir_login(parametros)
        return RETORNO_LOGIN.format(titulo="TikTok conectado", texto=limpar(
            (f"O corte-maker agora envia para {conta}. " if conta else "O corte-maker já pode enviar para o seu TikTok. ")
            + "Pode fechar esta aba e voltar para a interface."))
    except tiktok.Erro as erro:
        return RETORNO_LOGIN.format(titulo="Não deu para conectar", texto=limpar(erro))


# ---------------------------------------------------------------- HTTP

class Manipulador(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # silencioso: o registro que importa aparece na página
        pass

    # --- respostas
    def _json(self, dados: dict, status: int = 200) -> None:
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _html(self, texto: str) -> None:
        corpo = texto.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _pagina(self) -> None:
        """A interface, com os valores do `.env` (marca do canal, páginas do app do TikTok) já preenchidos."""
        corpo = _pagina_preenchida().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corpo)

    def _arquivo(self, caminho: Path, sem_cache: bool = False) -> None:
        """Serve um arquivo com suporte a Range (o player de vídeo precisa disso para avançar e voltar)."""
        tamanho = caminho.stat().st_size
        ini, fim, status = 0, tamanho - 1, 200
        faixa = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if faixa and (faixa[1] or faixa[2]):
            if faixa[1]:
                ini = int(faixa[1])
                fim = min(int(faixa[2]), tamanho - 1) if faixa[2] else tamanho - 1
            else:
                ini = max(0, tamanho - int(faixa[2]))
            if ini > fim:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{tamanho}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(caminho.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(fim - ini + 1))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {ini}-{fim}/{tamanho}")
        if sem_cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(caminho, "rb") as f:
            f.seek(ini)
            resta = fim - ini + 1
            while resta > 0:
                pedaco = f.read(min(262144, resta))
                if not pedaco:
                    break
                self.wfile.write(pedaco)
                resta -= len(pedaco)

    # --- entrada
    def _corpo_json(self) -> dict:
        try:
            dados = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except (ValueError, UnicodeDecodeError):
            raise Recusa(400, "Pedido inválido.")
        if not isinstance(dados, dict):
            raise Recusa(400, "Pedido inválido.")
        return dados

    def _receber_envio(self, nome: str) -> dict:
        """Guarda em trabalho/envios um vídeo arrastado para a página."""
        if not nome:
            raise Recusa(400, "Arquivo sem nome.")
        ENVIOS.mkdir(parents=True, exist_ok=True)
        destino = ENVIOS / pipeline.nome_arquivo(Path(nome).name, 120)
        resta = int(self.headers.get("Content-Length", 0))
        with open(destino, "wb") as f:
            while resta > 0:
                pedaco = self.rfile.read(min(1048576, resta))
                if not pedaco:
                    raise Recusa(400, "Envio interrompido.")
                f.write(pedaco)
                resta -= len(pedaco)
        return {"caminho": str(destino)}

    def _so_local(self) -> None:
        """Recusa pedidos vindos de outros sites ou de nomes que não sejam o próprio computador."""
        host = self.headers.get("Host", "")
        if host.split(":")[0] not in ("127.0.0.1", "localhost"):
            raise Recusa(403, "Host não permitido.")
        origem = self.headers.get("Origin")
        if self.command not in ("GET", "HEAD") and origem and urlparse(origem).netloc != host:
            raise Recusa(403, "Origem não permitida.")

    # --- despacho
    def _atender(self) -> None:
        url = urlparse(self.path)
        partes = [unquote(p) for p in url.path.split("/") if p]
        metodo = self.command
        try:
            self._so_local()
            if metodo in ("GET", "HEAD"):
                if not partes:
                    return self._pagina()
                if partes == ["api", "estado"]:
                    return self._json(_estado())
                if partes == ["api", "vozes"]:
                    return self._json(_vozes())
                if len(partes) == 4 and partes[:2] == ["api", "vozes"] and partes[3] == "amostra":
                    return self._arquivo(_amostra(partes[2]))
                if len(partes) == 5 and partes[:2] == ["api", "trabalhos"] and partes[3] == "previa":
                    return self._arquivo(_previa(partes[2], partes[4]), sem_cache=True)
                if len(partes) == 3 and partes[0] == "midia":
                    return self._arquivo(_dentro_da_saida(partes[1], partes[2]))
                if partes == ["tiktok", "retorno"]:
                    return self._html(_retorno_tiktok({k: v[0] for k, v in parse_qs(url.query).items()}))
                if partes == ["youtube", "retorno"]:
                    return self._html(_retorno_login({k: v[0] for k, v in parse_qs(url.query).items()}))
            elif metodo == "POST" and partes == ["api", "trabalhos"]:
                return self._json(_criar(self._corpo_json()))
            elif metodo == "POST" and len(partes) == 4 and partes[:2] == ["api", "trabalhos"] and partes[3] == "parar":
                return self._json(_parar(partes[2]))
            elif metodo == "POST" and len(partes) == 4 and partes[:2] == ["api", "trabalhos"] and partes[3] == "cortar":
                return self._json(_cortar(partes[2], self._corpo_json()))
            elif metodo == "POST" and len(partes) == 4 and partes[:2] == ["api", "trabalhos"] and partes[3] == "contexto":
                return self._json(_contexto(partes[2], self._corpo_json()))
            elif metodo == "POST" and len(partes) == 4 and partes[:2] == ["api", "trabalhos"] and partes[3] == "quadro":
                return self._json(_quadro_vivo(partes[2]))
            elif metodo == "POST" and partes == ["api", "cortes", "quadro"]:
                return self._json(_quadro_corte(self._corpo_json()))
            elif metodo == "POST" and partes == ["api", "cortes", "cancelar"]:
                return self._json(_cancelar_refazer(self._corpo_json()))
            elif metodo == "POST" and partes == ["api", "cortes", "refazer"]:
                return self._json(_refazer(self._corpo_json()))
            elif metodo == "POST" and partes == ["api", "quadro"]:
                return self._json(_quadro(self._corpo_json()))
            elif metodo == "POST" and partes == ["api", "compilacoes", "temas"]:
                return self._json(_propor_temas(self._corpo_json()))
            elif metodo == "POST" and partes == ["api", "compilacoes"]:
                return self._json(_compilar(self._corpo_json()))
            elif metodo == "POST" and partes == ["api", "youtube", "entrar"]:
                # o Google devolve o dono para este mesmo programa, na porta em que ele está aberto
                retorno = f"http://127.0.0.1:{self.server.server_address[1]}/youtube/retorno"
                return self._json({"url": _youtube(publicar.iniciar_login, retorno)})
            elif metodo == "POST" and partes == ["api", "tiktok", "chaves"]:
                d = self._corpo_json()
                _tiktok(tiktok.guardar_chaves, str(d.get("client_key", "")), str(d.get("client_secret", "")),
                        str(d.get("modo", "rascunho")))
                return self._json({"ok": True})
            elif metodo == "POST" and partes == ["api", "tiktok", "entrar"]:
                return self._json({"url": _tiktok(tiktok.iniciar_login, self.server.server_address[1])})
            elif metodo == "POST" and partes == ["api", "tiktok", "sair"]:
                tiktok.sair()
                return self._json({"ok": True})
            elif metodo == "POST" and partes == ["api", "tiktok", "privacidades"]:
                return self._json({"opcoes": _tiktok(tiktok.privacidades)})
            elif metodo == "POST" and partes == ["api", "tiktok", "publicar"]:
                return self._json({"id": _tiktok(tiktok.publicar, self._corpo_json(), SAIDA).id})
            elif metodo == "POST" and partes == ["api", "youtube", "sair"]:
                publicar.sair()
                return self._json({"ok": True})
            elif metodo == "POST" and partes == ["api", "youtube", "pasta"]:
                publicar.PASTA.mkdir(parents=True, exist_ok=True)  # a pasta onde o dono salva a credencial do Google
                os.startfile(publicar.PASTA) if sys.platform == "win32" else webbrowser.open(publicar.PASTA.as_uri())
                return self._json({"ok": True})
            elif metodo == "POST" and partes == ["api", "youtube", "publicar"]:
                return self._json({"id": _youtube(publicar.publicar, self._corpo_json(), SAIDA).id})
            elif metodo == "POST" and partes == ["api", "abrir"]:
                return self._json(_abrir(self._corpo_json()))
            elif metodo == "DELETE" and len(partes) == 3 and partes[:2] == ["api", "trabalhos"]:
                return self._json(_remover(partes[2]))
            elif metodo == "PUT" and partes == ["api", "envio"]:
                return self._json(self._receber_envio(parse_qs(url.query).get("nome", [""])[0]))
            raise Recusa(404, "Não encontrado.")
        except Recusa as r:
            self.close_connection = True  # o corpo do pedido pode não ter sido lido até o fim
            self._json({"detail": r.detalhe}, r.status)
        except CONEXAO_CAIU:
            self.close_connection = True  # o navegador desistiu (comum ao trocar de vídeo)
        except Exception as erro:
            self.close_connection = True
            try:
                self._json({"detail": f"Erro interno: {erro}"}, 500)
            except CONEXAO_CAIU:
                pass

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = _atender


def main() -> None:
    p = argparse.ArgumentParser(prog="cortes-ui", description="Abre a interface do corte-maker no navegador.")
    p.add_argument("--porta", type=int, default=int(config("PORT", "8765")))
    p.add_argument("--sem-abrir", action="store_true", help="não abre o navegador sozinho")
    a = p.parse_args()
    for fluxo in (sys.stdout, sys.stderr):
        fluxo.reconfigure(encoding="utf-8", errors="replace")

    # no Windows, reaproveitar endereço deixaria duas cópias do programa escutarem na mesma porta sem erro
    ThreadingHTTPServer.allow_reuse_address = sys.platform != "win32"
    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", a.porta), Manipulador)
    except OSError:
        sys.exit(f"A porta {a.porta} já está em uso. O corte-maker já está aberto? Senão, use --porta com outro número.")
    servidor.daemon_threads = True
    threading.Thread(target=_operario, daemon=True).start()
    global _lives_arquivo
    _lives_arquivo = RAIZ / "trabalho" / f"lives_no_ar_{a.porta}.json"
    if voltaram := _retomar_lives():
        print(f"{voltaram} live(s) retomada(s) de onde o programa tinha fechado.", flush=True)

    endereco = f"http://127.0.0.1:{a.porta}"
    print(f"corte-maker aberto em {endereco}  (Ctrl+C para fechar)", flush=True)
    if not a.sem_abrir:
        threading.Timer(0.6, webbrowser.open, args=(endereco,)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nFechado.")
