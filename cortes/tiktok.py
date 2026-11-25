"""Enviar cortes para o TikTok pela Content Posting API, com a conta do dono do programa.

O dono cria um app em developers.tiktok.com (produtos Login Kit, para computador, e Content Posting API), cadastra
o endereço de retorno `http://127.0.0.1:*/tiktok/retorno/` e cola na interface a Client key e o Client secret, que
ficam em `credenciais/tiktok.json`. No primeiro envio o navegador abre o login do TikTok; a autorização fica em
`credenciais/tiktok_token.json` (o acesso vale 2 horas e é renovado sozinho; a renovação vale 180 dias).

Dois jeitos de enviar:
  - rascunho (padrão): o vídeo chega na caixa de entrada do app do TikTok, e o dono termina a postagem por lá
    (legenda, som, capa). Funciona com o app ainda sem auditoria do TikTok;
  - direto: o vídeo é postado na hora, com a legenda. Enquanto o app não passa pela auditoria do TikTok, só dá para
    postar como privado (SELF_ONLY).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from . import pipeline

RAIZ = pipeline.RAIZ
PASTA = RAIZ / "credenciais"
CHAVES = PASTA / "tiktok.json"
TOKEN = PASTA / "tiktok_token.json"
REGISTRO = RAIZ / "canal" / "publicados_tiktok.json"

AUTORIZAR = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
RETORNO = "/tiktok/retorno/"  # o que o dono cadastra no app: http://127.0.0.1:*/tiktok/retorno/
MODOS = ("rascunho", "direto")
UM_PEDACO = 64_000_000  # até aqui o vídeo vai inteiro; acima, em pedaços (cada um de 5 a 64 MB, o último até 128)
PEDACO = 10_000_000


class Erro(Exception):
    """Algo que o dono precisa saber, já em frase pronta para a tela."""


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
    "access_token_invalid": "a autorização do TikTok venceu ou foi revogada; conecte a conta de novo",
    "scope_not_authorized": "a conta não deu essa permissão ao app; conecte de novo e aceite todas as permissões",
    "scope_permission_missed": "o app do TikTok não tem essa permissão liberada no developers.tiktok.com",
    "rate_limit_exceeded": "muitos envios seguidos; espere um minuto",
    "spam_risk_too_many_pending_share": "o TikTok só aceita 5 rascunhos pendentes por dia; publique ou apague os que estão na caixa de entrada do app",
    "spam_risk_too_many_posts": "a conta atingiu o limite de postagens do TikTok por hoje",
    "spam_risk_user_banned_from_posting": "o TikTok bloqueou postagens desta conta",
    "unaudited_client_can_only_post_to_private_accounts": "enquanto o app não passa pela auditoria do TikTok, o envio direto só funciona "
        "com a conta do TikTok inteira privada. Use o modo Rascunho (botão Chaves e modo): o vídeo vai para a caixa de "
        "entrada do app e você posta de lá, público",
    "privacy_level_option_mismatch": "essa privacidade não está disponível para esta conta",
    "invalid_grant": "a autorização do TikTok venceu; conecte a conta de novo",
    "invalid_client": "a Client key ou o Client secret não foram aceitos; confira as chaves do app",
    "access_denied": "a autorização foi negada na tela do TikTok",
}


def _motivo(corpo: bytes, status: int) -> str:
    try:
        dados = json.loads(corpo.decode("utf-8", "replace"))
    except ValueError:
        return f"resposta {status} do TikTok"
    erro = dados.get("error")
    if isinstance(erro, dict):
        codigo = str(erro.get("code", ""))
        return MOTIVOS.get(codigo) or " ".join(str(erro.get("message") or codigo or status).split())[:300]
    if isinstance(erro, str):  # o login devolve {"error": "...", "error_description": "..."}
        return MOTIVOS.get(erro) or str(dados.get("error_description") or erro)[:300]
    return f"resposta {status} do TikTok"


def _ok(corpo: bytes) -> bool:
    try:
        return (json.loads(corpo).get("error") or {}).get("code", "ok") == "ok"
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------- chaves e login

_trava = threading.RLock()
_logins: dict[str, tuple[str, str, float]] = {}  # state -> (verificador PKCE, endereço de retorno, quando)


def _ler(arquivo: Path) -> dict:
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def _gravar(arquivo: Path, dados) -> None:
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    tmp = arquivo.with_name(arquivo.name + ".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(arquivo)


def _chaves() -> dict:
    dados = _ler(CHAVES)
    if not dados.get("client_key") or not dados.get("client_secret"):
        raise Erro("Faltam as chaves do app do TikTok.")
    return dados


def guardar_chaves(client_key: str, client_secret: str, modo: str) -> None:
    """Guarda as chaves e o modo. Chaves em branco mantêm as que já estão guardadas (para só trocar o modo)."""
    atuais = _ler(CHAVES)
    client_key = client_key.strip() or atuais.get("client_key", "")
    client_secret = client_secret.strip() or atuais.get("client_secret", "")
    if not client_key or not client_secret:
        raise Erro("Cole a Client key e o Client secret do app do TikTok.")
    if modo not in MODOS:
        raise Erro("Escolha rascunho ou envio direto.")
    with _trava:
        mudou = _ler(CHAVES).get("client_key") != client_key
        _gravar(CHAVES, {"client_key": client_key, "client_secret": client_secret, "modo": modo})
        if mudou:  # outro app: a autorização antiga não vale para ele
            TOKEN.unlink(missing_ok=True)


def modo() -> str:
    return _ler(CHAVES).get("modo") if _ler(CHAVES).get("modo") in MODOS else "rascunho"


def _escopos() -> str:
    return "user.info.basic,video.upload" + (",video.publish" if modo() == "direto" else "")


def _token(campos: dict) -> dict:
    chaves = _chaves()
    status, _, corpo = _pedir("POST", f"{API}/oauth/token/", {"Content-Type": "application/x-www-form-urlencoded"},
                              urlencode(campos | {"client_key": chaves["client_key"],
                                                  "client_secret": chaves["client_secret"]}).encode(), 30)
    try:
        dados = json.loads(corpo)
    except ValueError:
        dados = {}
    if status != 200 or not dados.get("access_token"):
        raise Erro(f"O TikTok recusou o login: {_motivo(corpo, status)}.")
    return dados


def iniciar_login(porta: int) -> str:
    """O endereço do TikTok que o dono abre para autorizar."""
    chaves = _chaves()
    estado, verificador = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    desafio = hashlib.sha256(verificador.encode()).hexdigest()  # no computador, o TikTok pede o SHA256 em hexadecimal
    retorno = f"http://127.0.0.1:{porta}{RETORNO}"
    with _trava:
        for velho in [s for s, (_, _, t) in _logins.items() if time.time() - t > 900]:
            del _logins[velho]
        _logins[estado] = (verificador, retorno, time.time())
    return AUTORIZAR + "?" + urlencode({
        "client_key": chaves["client_key"], "scope": _escopos(), "response_type": "code", "redirect_uri": retorno,
        "state": estado, "code_challenge": desafio, "code_challenge_method": "S256"})


def _guardar_token(resposta: dict, extra: dict | None = None) -> dict:
    agora = time.time()
    dados = _ler(TOKEN) | (extra or {}) | {
        "access_token": resposta["access_token"], "refresh_token": resposta.get("refresh_token", ""),
        "open_id": resposta.get("open_id", ""), "escopos": resposta.get("scope", ""),
        "expira_em": agora + float(resposta.get("expires_in", 0)),
        "renovacao_expira_em": agora + float(resposta.get("refresh_expires_in", 0))}
    _gravar(TOKEN, dados)
    return dados


def concluir_login(parametros: dict) -> str:
    """Recebe a volta do TikTok, troca o código pela autorização e devolve o nome da conta conectada."""
    with _trava:
        pendente = _logins.pop(str(parametros.get("state", "")), None)
    if pendente is None:
        raise Erro("Este login não foi pedido por este programa, ou demorou demais. Clique em Conectar de novo.")
    if parametros.get("error"):
        raise Erro(f"O TikTok não autorizou: {MOTIVOS.get(parametros['error'], parametros.get('error_description') or parametros['error'])}.")
    verificador, retorno, _ = pendente
    resposta = _token({"grant_type": "authorization_code", "code": str(parametros.get("code", "")),
                       "redirect_uri": retorno, "code_verifier": verificador})
    with _trava:
        _gravar(TOKEN, {})
        _guardar_token(resposta, {"conta": ""})
    nome = ""
    try:
        status, _, corpo = _pedir("GET", f"{API}/user/info/?fields=display_name,username",
                                  {"Authorization": f"Bearer {_acesso()}"}, espera=30)
        usuario = (json.loads(corpo).get("data") or {}).get("user") or {} if status == 200 else {}
        nome = usuario.get("username") and f"@{usuario['username']}" or usuario.get("display_name", "")
    except (OSError, ValueError, http.client.HTTPException, Erro):
        pass
    if nome:
        with _trava:
            _gravar(TOKEN, _ler(TOKEN) | {"conta": nome})
    return nome


def _acesso() -> str:
    """Um token de acesso válido, renovado quando falta menos de um minuto para vencer."""
    with _trava:
        dados = _ler(TOKEN)
        if not dados.get("refresh_token"):
            raise Erro("A conta do TikTok não está conectada.")
        if dados.get("access_token") and dados.get("expira_em", 0) - time.time() > 60:
            return dados["access_token"]
        try:
            resposta = _token({"grant_type": "refresh_token", "refresh_token": dados["refresh_token"]})
        except Erro:
            TOKEN.unlink(missing_ok=True)  # a renovação venceu ou foi revogada: precisa conectar de novo
            raise Erro("A autorização do TikTok venceu. Conecte a conta de novo.") from None
        return _guardar_token(resposta)["access_token"]  # a renovação também troca o refresh_token


def sair() -> None:
    with _trava:
        TOKEN.unlink(missing_ok=True)


def _api(caminho: str, corpo: dict, espera: float = 60) -> dict:
    status, _, resposta = _pedir("POST", f"{API}{caminho}", {
        "Authorization": f"Bearer {_acesso()}", "Content-Type": "application/json; charset=UTF-8"},
        json.dumps(corpo, ensure_ascii=False).encode("utf-8"), espera)
    if status != 200 or not _ok(resposta):
        raise Erro(f"O TikTok recusou: {_motivo(resposta, status)}.")
    return json.loads(resposta).get("data") or {}


def privacidades() -> list[str]:
    """As privacidades que a conta aceita no envio direto (o TikTok exige consultar antes de postar)."""
    return list(_api("/post/publish/creator_info/query/", {}).get("privacy_level_options") or [])


# ---------------------------------------------------------------- envio

@dataclass
class Envio:
    id: str
    pasta: str
    arquivo: str
    modo: str
    privacidade: str = ""
    estado: str = "enviando"  # enviando | processando | pronto | erro
    pct: float = 0.0
    publish_id: str = ""
    situacao: str = ""  # o status que o TikTok informa
    erro: str = ""
    criado: float = field(default_factory=time.time)

    @property
    def chave(self) -> str:
        return f"{self.pasta}/{self.arquivo}"


envios: dict[str, Envio] = {}


def publicar(pedido: dict, saida: Path) -> Envio:
    nome_pasta, arquivo = str(pedido.get("pasta", "")), str(pedido.get("arquivo", ""))
    video = (saida / nome_pasta / arquivo).resolve()
    if not nome_pasta or not arquivo or video.parent.parent != saida.resolve() or video.suffix.lower() != ".mp4":
        raise Erro("Pedido de envio inválido.")
    if not video.is_file():
        raise Erro("O arquivo do vídeo não está mais na pasta.")
    jeito = modo()
    legenda = " ".join(str(pedido.get("legenda", "")).split())[:2200]
    privacidade = str(pedido.get("privacidade", ""))
    _acesso()  # sem conta conectada, a recusa sai agora e não no meio do envio
    if jeito == "direto":
        opcoes = privacidades()
        if privacidade not in opcoes:
            raise Erro("Escolha uma das privacidades que o TikTok oferece para esta conta.")
    envio = Envio(uuid.uuid4().hex[:10], nome_pasta, arquivo, jeito, privacidade)
    with _trava:
        if any(e.chave == envio.chave and e.estado in ("enviando", "processando") for e in envios.values()):
            raise Erro("Este vídeo já está sendo enviado ao TikTok.")
        envios[envio.id] = envio
    threading.Thread(target=_rodar, args=(envio, video, legenda), daemon=True).start()
    return envio


def _rodar(envio: Envio, video: Path, legenda: str) -> None:
    try:
        total = video.stat().st_size
        tamanho = total if total <= UM_PEDACO else PEDACO
        pedacos = max(1, total // tamanho)
        fonte = {"source": "FILE_UPLOAD", "video_size": total, "chunk_size": tamanho, "total_chunk_count": pedacos}
        if envio.modo == "direto":
            dados = _api("/post/publish/video/init/", {"source_info": fonte, "post_info": {
                "title": legenda, "privacy_level": envio.privacidade, "disable_comment": False,
                "disable_duet": False, "disable_stitch": False, "video_cover_timestamp_ms": 1000}})
        else:
            dados = _api("/post/publish/inbox/video/init/", {"source_info": fonte})
        envio.publish_id, destino = str(dados.get("publish_id", "")), str(dados.get("upload_url", ""))
        if not destino:
            raise Erro("O TikTok não devolveu o endereço de envio.")
        with video.open("rb") as f:
            for n in range(pedacos):
                inicio = n * tamanho
                fim = total if n == pedacos - 1 else inicio + tamanho  # o último pedaço leva a sobra
                f.seek(inicio)
                parte = f.read(fim - inicio)
                for tentativa in range(4):
                    try:
                        status, _, resposta = _pedir("PUT", destino, {
                            "Content-Type": "video/mp4", "Content-Length": str(len(parte)),
                            "Content-Range": f"bytes {inicio}-{fim - 1}/{total}"}, parte, 300)
                    except (OSError, http.client.HTTPException):
                        status, resposta = 0, b""
                    if status in (200, 201, 206):
                        break
                    if status not in (0, 500, 502, 503, 504) or tentativa == 3:
                        raise Erro("O TikTok interrompeu o envio" + (": o endereço de envio venceu" if status == 403 else
                                                                     f" ({_motivo(resposta, status)})") + ".")
                    time.sleep(2 ** (tentativa + 1))
                envio.pct = round(100 * fim / total, 1)
        envio.estado = "processando"
        for _ in range(60):  # o TikTok processa o vídeo e avisa quando chegou na caixa de entrada (ou foi postado)
            time.sleep(4)
            situacao = _api("/post/publish/status/fetch/", {"publish_id": envio.publish_id})
            envio.situacao = str(situacao.get("status", ""))
            if envio.situacao == "FAILED":
                raise Erro(f"O TikTok não aceitou o vídeo ({situacao.get('fail_reason') or 'sem motivo informado'}).")
            if envio.situacao in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                break
        _registrar(envio)
        envio.estado = "pronto"
    except Erro as erro:
        envio.estado, envio.erro = "erro", str(erro)
    except Exception as erro:  # a thread não pode morrer calada
        envio.estado, envio.erro = "erro", f"Erro inesperado no envio ao TikTok: {str(erro).strip() or type(erro).__name__}"


def enviados() -> dict[str, dict]:
    lista = _ler_lista()
    return {f"{p['pasta']}/{p['arquivo']}": p for p in lista if isinstance(p, dict) and p.get("pasta") and p.get("arquivo")}


def _ler_lista() -> list:
    try:
        lista = json.loads(REGISTRO.read_text(encoding="utf-8"))
        return lista if isinstance(lista, list) else []
    except (OSError, ValueError):
        return []


def _registrar(envio: Envio) -> None:
    registro = {k: v for k, v in asdict(envio).items() if k in ("pasta", "arquivo", "modo", "privacidade", "publish_id", "situacao")}
    registro["enviado_em"] = time.time()
    with _trava:
        _gravar(REGISTRO, [*_ler_lista(), registro])


def estado() -> dict:
    chaves = _ler(CHAVES)
    token = _ler(TOKEN)
    return {
        "configurado": bool(chaves.get("client_key") and chaves.get("client_secret")), "modo": modo(),
        # o envio direto precisa da permissão video.publish: conectada no modo rascunho, a conta precisa conectar de novo
        "conectado": bool(token.get("refresh_token")) and (modo() != "direto" or "video.publish" in token.get("escopos", "")),
        "conta": token.get("conta", ""),
        "envios": [asdict(e) for e in sorted(envios.values(), key=lambda e: e.criado, reverse=True)][:20],
        "enviados": {k: {c: p.get(c) for c in ("modo", "privacidade", "enviado_em")} for k, p in enviados().items()},
    }
