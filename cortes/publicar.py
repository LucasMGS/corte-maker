"""Publicar no YouTube pelo canal do dono do programa: login OAuth, envio do vídeo, da ficha e da miniatura.

Só biblioteca padrão. O dono cria uma credencial "App para computador" no Google Cloud e salva o arquivo em
`credenciais/`; no primeiro uso o navegador abre o login do Google e a autorização fica guardada em
`credenciais/youtube_token.json`. O que foi publicado fica em `canal/publicados.json` e em `canal/resultados.md`.

O envio é o "resumable upload" da YouTube Data API: abre uma sessão com a ficha do vídeo, manda o arquivo em
pedaços e, se a conexão cair, pergunta ao servidor até onde chegou e continua dali.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from . import compilar, pipeline

RAIZ = pipeline.RAIZ
PASTA = RAIZ / "credenciais"
TOKEN = PASTA / "youtube_token.json"
REGISTRO = RAIZ / "canal" / "publicados.json"
RESULTADOS = RAIZ / "canal" / "resultados.md"
ESCOPOS = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
CATEGORIA_JOGOS = "20"
PEDACO = 8 * 1024 * 1024  # múltiplo de 256 KB, como o YouTube exige de cada pedaço menos o último
TENTATIVAS = 6
PRIVACIDADES = {"privado": "private", "nao_listado": "unlisted", "publico": "public", "agendado": "private"}
ROTULOS = {"privado": "privado", "nao_listado": "não listado", "publico": "público", "agendado": "agendado"}

# os endereços do Google; nos testes, a variável de ambiente aponta tudo para um servidor de mentira
_TESTE = os.environ.get("CORTES_YOUTUBE_TESTE", "").rstrip("/")
AUTORIZAR = (_TESTE or "https://accounts.google.com") + "/o/oauth2/v2/auth"
TROCAR = (_TESTE or "https://oauth2.googleapis.com") + "/token"
API = (_TESTE or "https://www.googleapis.com") + "/youtube/v3"
ENVIO = (_TESTE or "https://www.googleapis.com") + "/upload/youtube/v3"


class Erro(Exception):
    """Algo que o dono do canal precisa saber, já em frase pronta para a tela."""


# ---------------------------------------------------------------- HTTP

def _pedir(metodo: str, url: str, cabecalhos: dict | None = None, corpo: bytes | None = None,
           espera: float = 120) -> tuple[int, dict, bytes]:
    u = urlsplit(url)
    conexao = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.netloc, timeout=espera)
    try:
        conexao.request(metodo, u.path + (f"?{u.query}" if u.query else ""), body=corpo, headers=cabecalhos or {})
        r = conexao.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        conexao.close()


MOTIVOS = {
    "quotaExceeded": "a cota diária da API do YouTube acabou; ela volta à meia-noite do horário do Pacífico",
    "uploadLimitExceeded": "o canal atingiu o limite de envios do YouTube por hoje",
    "youtubeSignupRequired": "a conta do Google conectada não tem canal no YouTube; desconecte e conecte com a conta dona do canal",
    "invalidTitle": "o YouTube recusou o título (vazio, longo demais ou com < e >)",
    "invalidDescription": "o YouTube recusou a descrição (longa demais ou com < e >)",
    "invalidPublishAt": "o YouTube recusou o horário do agendamento",
    "invalid_grant": "a autorização do YouTube venceu ou foi revogada; conecte o canal de novo",
    "invalid_client": "a credencial do Google não foi aceita; confira o arquivo em credenciais/",
    "access_denied": "a autorização foi negada na tela do Google",
}


def _motivo(corpo: bytes, status: int) -> str:
    """A explicação de um erro do Google, em português quando é um dos casos conhecidos."""
    try:
        erro = json.loads(corpo.decode("utf-8", "replace")).get("error")
    except ValueError:
        erro = None
    if isinstance(erro, dict):
        razao = next((e.get("reason", "") for e in erro.get("errors") or [] if isinstance(e, dict)), "")
        return MOTIVOS.get(razao) or " ".join(str(erro.get("message") or razao or status).split())[:300]
    if isinstance(erro, str):
        return MOTIVOS.get(erro) or erro
    return f"resposta {status} do YouTube"


# ---------------------------------------------------------------- credencial e login

_trava = threading.RLock()
_logins: dict[str, tuple[str, str, float]] = {}  # state -> (verificador PKCE, endereço de retorno, quando)


def _credencial() -> dict:
    """O `client_id` e o `client_secret` do arquivo que o dono baixou do Google Cloud."""
    arquivos = sorted(PASTA.glob("*client_secret*.json")) if PASTA.is_dir() else []
    if not arquivos:
        raise Erro("Falta a credencial do Google: salve o arquivo client_secret baixado do Google Cloud na pasta credenciais.")
    try:
        dados = json.loads(arquivos[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Erro(f"Não consegui ler {arquivos[0].name}. Baixe o arquivo de novo no Google Cloud.") from None
    if "installed" not in dados:
        raise Erro("A credencial em credenciais/ não é do tipo \"App para computador\". Crie outra com esse tipo no Google Cloud.")
    dados = dados["installed"]
    if not dados.get("client_id") or not dados.get("client_secret"):
        raise Erro(f"{arquivos[0].name} está incompleto. Baixe o arquivo de novo no Google Cloud.")
    return {"client_id": dados["client_id"], "client_secret": dados["client_secret"]}


def _ler_token() -> dict:
    try:
        dados = json.loads(TOKEN.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def _gravar(arquivo: Path, dados) -> None:
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    tmp = arquivo.with_name(arquivo.name + ".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(arquivo)


def _token(campos: dict) -> dict:
    status, _, corpo = _pedir("POST", TROCAR, {"Content-Type": "application/x-www-form-urlencoded"},
                              urlencode(campos | _credencial()).encode(), 30)
    if status != 200:
        raise Erro(f"O Google recusou o login: {_motivo(corpo, status)}.")
    return json.loads(corpo)


def iniciar_login(retorno: str) -> str:
    """O endereço do Google que o dono abre para autorizar. `retorno` é a página do programa que recebe a resposta."""
    credencial = _credencial()
    estado, verificador = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    desafio = base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest()).rstrip(b"=").decode()
    with _trava:
        for velho in [s for s, (_, _, t) in _logins.items() if time.time() - t > 900]:
            del _logins[velho]
        _logins[estado] = (verificador, retorno, time.time())
    return AUTORIZAR + "?" + urlencode({
        "client_id": credencial["client_id"], "redirect_uri": retorno, "response_type": "code", "scope": ESCOPOS,
        "access_type": "offline", "prompt": "consent", "state": estado,
        "code_challenge": desafio, "code_challenge_method": "S256"})


def concluir_login(parametros: dict) -> str:
    """Recebe a volta do Google, troca o código pela autorização e devolve o nome do canal conectado."""
    with _trava:
        pendente = _logins.pop(str(parametros.get("state", "")), None)
    if pendente is None:
        raise Erro("Este login não foi pedido por este programa, ou demorou demais. Clique em Conectar de novo.")
    if parametros.get("error"):
        raise Erro(f"O Google não autorizou: {MOTIVOS.get(parametros['error'], parametros['error'])}.")
    verificador, retorno, _ = pendente
    resposta = _token({"grant_type": "authorization_code", "code": str(parametros.get("code", "")),
                       "redirect_uri": retorno, "code_verifier": verificador})
    if not resposta.get("refresh_token"):
        raise Erro("O Google não devolveu a autorização permanente. Tire o acesso do app em myaccount.google.com/permissions e conecte de novo.")
    dados = {"refresh_token": resposta["refresh_token"], "access_token": resposta.get("access_token", ""),
             "expira_em": time.time() + float(resposta.get("expires_in", 0)), "canal": {}}
    with _trava:
        _gravar(TOKEN, dados)
    status = 0
    try:
        status, _, corpo = _pedir("GET", f"{API}/channels?part=snippet&mine=true", {"Authorization": f"Bearer {_acesso()}"}, espera=30)
        itens = json.loads(corpo).get("items") or [] if status == 200 else []
    except (OSError, ValueError, http.client.HTTPException):
        itens = []
    if itens:
        dados["canal"] = {"id": itens[0].get("id", ""), "titulo": (itens[0].get("snippet") or {}).get("title", "")}
        with _trava:
            _gravar(TOKEN, _ler_token() | {"canal": dados["canal"]})
    elif status == 200:  # a conta entrou, mas não tem canal: melhor recusar agora do que no meio do primeiro envio
        sair()
        raise Erro("Esta conta do Google não tem canal no YouTube. Conecte de novo e escolha a conta dona do canal "
                   "(se o Google mostrar uma lista de canais, escolha o seu).")
    return dados["canal"].get("titulo", "")


def _acesso() -> str:
    """Um token de acesso válido, renovado quando falta menos de um minuto para vencer."""
    with _trava:
        dados = _ler_token()
        if not dados.get("refresh_token"):
            raise Erro("O canal do YouTube não está conectado.")
        if dados.get("access_token") and dados.get("expira_em", 0) - time.time() > 60:
            return dados["access_token"]
        try:
            resposta = _token({"grant_type": "refresh_token", "refresh_token": dados["refresh_token"]})
        except Erro as erro:
            if "venceu ou foi revogada" in str(erro):
                TOKEN.unlink(missing_ok=True)
            raise
        dados |= {"access_token": resposta["access_token"], "expira_em": time.time() + float(resposta.get("expires_in", 0))}
        _gravar(TOKEN, dados)
        return dados["access_token"]


def sair() -> None:
    """Esquece a autorização neste computador (o acesso do app também pode ser tirado na conta do Google)."""
    with _trava:
        TOKEN.unlink(missing_ok=True)


# ---------------------------------------------------------------- envio

def _enviar(video: Path, ficha: dict, andamento) -> dict:
    """Manda `video` com a `ficha` (o recurso `video` da API) e devolve o que o YouTube criou.
    `andamento(bytes_enviados, total)` é chamado a cada pedaço."""
    total = video.stat().st_size
    corpo = json.dumps(ficha, ensure_ascii=False).encode("utf-8")
    status, cab, resposta = _pedir("POST", f"{ENVIO}/videos?uploadType=resumable&part=snippet,status", {
        "Authorization": f"Bearer {_acesso()}", "Content-Type": "application/json; charset=UTF-8",
        "Content-Length": str(len(corpo)), "X-Upload-Content-Length": str(total), "X-Upload-Content-Type": "video/*"}, corpo, 60)
    sessao = cab.get("location", "")
    if status != 200 or not sessao:
        raise Erro(f"O YouTube não aceitou o envio: {_motivo(resposta, status)}.")

    def recebido(cabecalhos: dict) -> int:
        faixa = re.fullmatch(r"bytes=0-(\d+)", cabecalhos.get("range", "").strip())
        return int(faixa[1]) + 1 if faixa else 0

    enviado, falhas = 0, 0
    with video.open("rb") as f:
        while True:
            f.seek(enviado)
            pedaco = f.read(PEDACO)
            try:
                status, cab, resposta = _pedir("PUT", sessao, {
                    "Authorization": f"Bearer {_acesso()}", "Content-Type": "video/*", "Content-Length": str(len(pedaco)),
                    "Content-Range": f"bytes {enviado}-{enviado + len(pedaco) - 1}/{total}"}, pedaco, 300)
            except (OSError, http.client.HTTPException):
                status = 0
            if status in (200, 201):
                andamento(total, total)
                return json.loads(resposta)
            if status == 308:
                enviado, falhas = recebido(cab), 0
                andamento(enviado, total)
                continue
            if status not in (0, 500, 502, 503, 504):
                raise Erro(f"O YouTube interrompeu o envio: {_motivo(resposta, status)}.")
            falhas += 1
            if falhas > TENTATIVAS:
                raise Erro("A conexão com o YouTube caiu vezes demais no meio do envio. Tente de novo mais tarde.")
            time.sleep(min(60, 2 ** falhas))
            try:  # antes de continuar, pergunta até onde o YouTube recebeu
                status, cab, resposta = _pedir("PUT", sessao, {
                    "Authorization": f"Bearer {_acesso()}", "Content-Length": "0", "Content-Range": f"bytes */{total}"}, b"", 60)
            except (OSError, http.client.HTTPException):
                continue
            if status in (200, 201):
                andamento(total, total)
                return json.loads(resposta)
            if status == 308:
                enviado = recebido(cab)
            elif status == 404:
                raise Erro("A sessão de envio venceu no YouTube. Publique de novo.")


def _miniatura(video_id: str, imagem: Path) -> None:
    status, _, resposta = _pedir("POST", f"{ENVIO}/thumbnails/set?videoId={video_id}&uploadType=media", {
        "Authorization": f"Bearer {_acesso()}", "Content-Type": "image/jpeg", "Content-Length": str(imagem.stat().st_size)},
        imagem.read_bytes(), 120)
    if status == 403:
        raise Erro("o canal não pode usar miniatura personalizada; verifique o canal por telefone em youtube.com/verify")
    if status != 200:
        raise Erro(_motivo(resposta, status))


# ---------------------------------------------------------------- o que publicar

@dataclass
class Envio:
    id: str
    tipo: str  # compilacao | corte
    pasta: str
    arquivo: str
    titulo: str
    privacidade: str
    publicar_em: str = ""  # agendado: quando o YouTube torna público, em ISO 8601 UTC
    miniatura: str = ""
    estado: str = "enviando"  # enviando | miniatura | pronto | erro
    pct: float = 0.0
    video_id: str = ""
    url: str = ""
    erro: str = ""
    aviso: str = ""
    criado: float = field(default_factory=time.time)

    @property
    def chave(self) -> str:
        return f"{self.pasta}/{self.arquivo}"


envios: dict[str, Envio] = {}


def _limpo(texto: str, limite: int) -> str:
    """O YouTube recusa < e > em título e descrição."""
    return texto.replace("<", "‹").replace(">", "›").strip()[:limite]


def _material(tipo: str, pasta: Path, arquivo: str) -> dict:
    """Título padrão, descrição, etiquetas e miniaturas possíveis do que vai ser publicado."""
    if tipo == "compilacao":
        try:
            m = json.loads((pasta / compilar.MANIFESTO).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Erro("Não achei esta compilação.") from None
        capas = [n for n in dict.fromkeys([m.get("miniatura"), *m.get("miniaturas_opcoes", []), m.get("miniatura_limpa")])
                 if n and (pasta / n).is_file()]
        return {"arquivo": m["arquivo"], "titulo": m["titulo"], "descricao": m.get("descricao", ""), "capas": capas}
    sessao = pipeline.ler_manifesto(pasta) or {}
    corte = next((c for c in sessao.get("cortes", []) if c.get("arquivo") == arquivo), None)
    if corte is None:
        raise Erro("Não achei este corte.")
    hashtags = " ".join("#" + str(h).lstrip("#") for h in corte.get("hashtags") or [])
    descricao = "\n".join(x for x in (corte.get("descricao", ""), f"Créditos: {sessao['canal']}" if sessao.get("canal") else "", hashtags) if x)
    return {"arquivo": arquivo, "titulo": corte.get("titulo") or Path(arquivo).stem, "descricao": descricao, "capas": []}


def publicar(pedido: dict, saida: Path) -> Envio:
    """Confere o pedido da página e começa o envio numa thread própria. Devolve o envio, que a página acompanha."""
    tipo = str(pedido.get("tipo", ""))
    nome_pasta = str(pedido.get("pasta", ""))
    pasta = (saida / nome_pasta).resolve()
    if tipo not in ("compilacao", "corte") or not nome_pasta or pasta.parent != saida.resolve() or not pasta.is_dir():
        raise Erro("Pedido de publicação inválido.")
    material = _material(tipo, pasta, str(pedido.get("arquivo", "")))
    video = pasta / material["arquivo"]
    if not video.is_file():
        raise Erro("O arquivo do vídeo não está mais na pasta.")
    titulo = _limpo(" ".join(str(pedido.get("titulo") or material["titulo"]).split()), 100)
    if not titulo:
        raise Erro("O vídeo precisa de um título.")
    privacidade = str(pedido.get("privacidade", "privado"))
    if privacidade not in PRIVACIDADES:
        raise Erro("Escolha privado, não listado, agendado ou público.")
    publicar_em = ""
    if privacidade == "agendado":
        try:
            quando = datetime.fromisoformat(str(pedido.get("publicar_em", "")).replace("Z", "+00:00"))
            quando = (quando if quando.tzinfo else quando.astimezone()).astimezone(timezone.utc)
        except ValueError:
            raise Erro("Diga o dia e a hora do agendamento.") from None
        if quando < datetime.now(timezone.utc) + timedelta(minutes=10):
            raise Erro("O agendamento precisa ser para daqui a pelo menos 10 minutos.")
        publicar_em = quando.strftime("%Y-%m-%dT%H:%M:%SZ")
    miniatura = str(pedido.get("miniatura", ""))
    if miniatura and miniatura not in material["capas"]:
        raise Erro("Esta miniatura não é desta compilação.")
    _acesso()  # sem canal conectado, a recusa sai agora e não no meio do envio
    envio = Envio(uuid.uuid4().hex[:10], tipo, nome_pasta, material["arquivo"], titulo, privacidade, publicar_em, miniatura)
    with _trava:
        if any(e.chave == envio.chave and e.estado in ("enviando", "miniatura") for e in envios.values()):
            raise Erro("Este vídeo já está sendo enviado.")
        if envio.chave in publicados() and not pedido.get("de_novo"):
            raise Erro("Este vídeo já foi publicado.")
        envios[envio.id] = envio
    etiquetas = list(dict.fromkeys(re.findall(r"#(\w+)", material["descricao"])))[:15]
    ficha = {"snippet": {"title": titulo, "description": _limpo(material["descricao"], 4900), "tags": etiquetas,
                         "categoryId": CATEGORIA_JOGOS},
             "status": {"privacyStatus": PRIVACIDADES[privacidade], "selfDeclaredMadeForKids": False}
             | ({"publishAt": publicar_em} if publicar_em else {})}
    threading.Thread(target=_rodar, args=(envio, video, ficha, pasta / miniatura if miniatura else None), daemon=True).start()
    return envio


def _rodar(envio: Envio, video: Path, ficha: dict, capa: Path | None) -> None:
    try:
        def andamento(feito: int, total: int) -> None:
            envio.pct = round(100 * feito / max(total, 1), 1)

        criado = _enviar(video, ficha, andamento)
        envio.video_id = str(criado.get("id", ""))
        if not envio.video_id:
            raise Erro("O YouTube recebeu o arquivo mas não devolveu o endereço do vídeo. Confira no YouTube Studio.")
        envio.url = f"https://youtu.be/{envio.video_id}"
        if capa is not None:
            envio.estado = "miniatura"
            try:
                _miniatura(envio.video_id, capa)
            except (Erro, OSError, http.client.HTTPException) as erro:  # o vídeo já está lá: a miniatura vira aviso
                envio.aviso = f"O vídeo subiu, mas a miniatura não: {str(erro) or type(erro).__name__}. Envie a miniatura pelo YouTube Studio."
        _registrar(envio)
        envio.estado = "pronto"
    except Erro as erro:
        envio.estado, envio.erro = "erro", str(erro)
    except Exception as erro:  # a thread não pode morrer calada
        envio.estado, envio.erro = "erro", f"Erro inesperado no envio: {str(erro).strip() or type(erro).__name__}"


# ---------------------------------------------------------------- registro do que foi publicado

def publicados() -> dict[str, dict]:
    """O que já foi publicado, por "pasta/arquivo"."""
    try:
        lista = json.loads(REGISTRO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {f"{p['pasta']}/{p['arquivo']}": p for p in lista if isinstance(p, dict) and p.get("pasta") and p.get("arquivo")}


def _registrar(envio: Envio) -> None:
    registro = {k: v for k, v in asdict(envio).items() if k in (
        "tipo", "pasta", "arquivo", "titulo", "privacidade", "publicar_em", "miniatura", "video_id", "url")}
    registro["enviado_em"] = time.time()
    with _trava:
        try:
            lista = json.loads(REGISTRO.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            lista = []
        _gravar(REGISTRO, [*(lista if isinstance(lista, list) else []), registro])
        try:
            _anotar(registro)
        except OSError:
            pass  # o caderno de resultados é apoio: sem ele, o registro acima já guarda tudo


def _anotar(r: dict) -> None:
    """Acrescenta a publicação ao bloco do vídeo em `canal/resultados.md` (ou abre um bloco novo no fim)."""
    quando = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["enviado_em"]))
    situacao = ROTULOS[r["privacidade"]] + (f" para {r['publicar_em']}" if r["publicar_em"] else "")
    linha = f"- Publicado em {quando} ({situacao}): {r['url']} | título usado: {r['titulo']}" + (f" | miniatura: {r['miniatura']}" if r["miniatura"] else "")
    texto = RESULTADOS.read_text(encoding="utf-8") if RESULTADOS.is_file() else "# Resultados dos vídeos\n"
    marca = f"- Pasta: saida\\{r['pasta']}"
    linhas = texto.split("\n")
    onde = next((n for n, l in enumerate(linhas) if l.strip() == marca), None) if r["tipo"] == "compilacao" else None
    if onde is not None:
        linhas.insert(onde + 1, linha)
        texto = "\n".join(linhas)
    else:
        caminho = r["pasta"] if r["tipo"] == "compilacao" else f"{r['pasta']}\\{r['arquivo']}"
        texto = texto.rstrip("\n") + f"\n\n## {r['titulo']}\n- Pasta: saida\\{caminho}\n{linha}\n"
    RESULTADOS.parent.mkdir(parents=True, exist_ok=True)
    RESULTADOS.write_text(texto, encoding="utf-8")


# ---------------------------------------------------------------- o que a página mostra

def estado() -> dict:
    try:
        _credencial()
        problema = ""
    except Erro as erro:
        problema = str(erro)
    token = _ler_token()
    return {
        "configurado": not problema, "problema": problema, "conectado": bool(token.get("refresh_token")),
        "canal": (token.get("canal") or {}).get("titulo", ""), "pasta_credenciais": str(PASTA),
        "envios": [asdict(e) for e in sorted(envios.values(), key=lambda e: e.criado, reverse=True)][:20],
        "publicados": {k: {c: p.get(c) for c in ("url", "titulo", "privacidade", "publicar_em", "enviado_em")}
                       for k, p in publicados().items()},
    }
