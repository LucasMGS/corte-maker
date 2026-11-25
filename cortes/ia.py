"""Chamada ao Claude para escolher os momentos.

Dois caminhos, nesta ordem:
  1. API da Anthropic (SDK oficial), se ANTHROPIC_API_KEY estiver no ambiente ou no `.env`;
  2. Claude Code CLI (`claude -p`), usando o login/assinatura já feito na máquina.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from . import gasto
from .config import config

MODELO_API = "claude-opus-5-5"
MODELO_TRIAGEM = "claude-haiku-5-5"  # filtro barato do modo ao vivo: decide se o trecho merece a análise completa
ESFORCO_TRIAGEM = "low"  # quanto o modelo da triagem pensa antes de responder

SISTEMA = (
    "Você é um editor profissional de cortes para YouTube Shorts e TikTok, especializado em lives e "
    "vídeos de games. Recebe a transcrição numerada de um trecho de vídeo e escolhe os momentos com "
    "maior potencial de engajamento como vídeo curto vertical."
)

ESQUEMA = {
    "type": "object",
    "properties": {
        "momentos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "linha_inicio": {"type": "integer"},
                    "linha_fim": {"type": "integer"},
                    "nota": {"type": "integer"},
                    "titulo": {"type": "string"},
                    "gancho": {"type": "string"},
                    "descricao": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "motivo": {"type": "string"},
                    "abertura": {"type": "string"},
                    "fecho": {"type": "string"},
                },
                "required": ["linha_inicio", "linha_fim", "nota", "titulo", "gancho", "descricao", "hashtags", "motivo",
                             "abertura", "fecho"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["momentos"],
    "additionalProperties": False,
}


SISTEMA_TRADUCAO = (
    "Você é tradutor de legendas de vídeos de games e esports. Traduz de forma natural e curta, como se fala, "
    "mantendo nomes próprios, apelidos de jogadores e termos do jogo."
)

ESQUEMA_TRADUCAO = {
    "type": "object",
    "properties": {"traducoes": {"type": "array", "items": {"type": "string"}}},
    "required": ["traducoes"],
    "additionalProperties": False,
}


SISTEMA_TRIAGEM = (
    "Você faz a triagem de trechos de lives para um editor de cortes de YouTube Shorts e TikTok. Recebe a "
    "transcrição numerada de alguns minutos de live e diz se há ali algum momento que mereça virar corte."
)

ESQUEMA_TRIAGEM = {
    "type": "object",
    "properties": {"nota": {"type": "integer"}, "motivo": {"type": "string"}},
    "required": ["nota", "motivo"],
    "additionalProperties": False,
}


SISTEMA_TEMAS = (
    "Você é o editor-chefe de um canal de cortes de games no YouTube. Recebe a lista dos cortes curtos que o canal "
    "já tem prontos e monta, com eles, compilações temáticas para publicar como vídeo normal, mais longo."
)

ESQUEMA_TEMAS = {
    "type": "object",
    "properties": {
        "temas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tema": {"type": "string"},
                    "titulo": {"type": "string"},
                    "titulos_ab": {"type": "array", "items": {"type": "string"}},
                    "descricao": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "cortes": {"type": "array", "items": {"type": "integer"}},
                    "corte_capa": {"type": "integer"},
                    "texto_capa": {"type": "string"},
                    "motivo": {"type": "string"},
                },
                "required": ["tema", "titulo", "titulos_ab", "descricao", "hashtags", "cortes", "corte_capa", "texto_capa",
                             "motivo"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["temas"],
    "additionalProperties": False,
}


class IAErro(RuntimeError):
    pass


def backend() -> str | None:
    if config("ANTHROPIC_API_KEY"):
        return "api"
    if shutil.which("claude"):
        return "cli"
    return None


ultima_falha: str | None = None  # a interface mostra isto no indicador de IA


def pedir_momentos(prompt: str, modelo: str | None) -> list[dict]:
    global ultima_falha
    try:
        momentos = _pedir(prompt, modelo, SISTEMA, ESQUEMA, "analise").get("momentos")
        if not isinstance(momentos, list):
            raise IAErro("resposta da IA fora do formato esperado")
    except IAErro as erro:
        ultima_falha = str(erro)
        raise
    ultima_falha = None
    return momentos


def pedir_temas(prompt: str, modelo: str | None) -> list[dict]:
    """Grupos temáticos de cortes para compilação. Os números em `cortes` e `corte_capa` são os da lista enviada
    no prompt; quem chama confere se existem."""
    global ultima_falha
    try:
        temas = _pedir(prompt, modelo, SISTEMA_TEMAS, ESQUEMA_TEMAS, "temas").get("temas")
        if not isinstance(temas, list):
            raise IAErro("resposta da IA fora do formato esperado")
    except IAErro as erro:
        ultima_falha = str(erro)
        raise
    ultima_falha = None
    return [t for t in temas if isinstance(t, dict)]


def modelo_barato(modelo: str | None) -> bool:
    """A análise já roda no modelo da triagem: filtrar antes com ele mesmo não economizaria nada."""
    return "haiku" in (modelo or "").lower()


def triar(prompt: str) -> tuple[int, str]:
    """Nota de 0 a 100 do melhor momento do trecho, dada pelo modelo barato, e uma frase dizendo qual é."""
    resposta = _pedir(prompt, MODELO_TRIAGEM, SISTEMA_TRIAGEM, ESQUEMA_TRIAGEM, "triagem")
    try:
        nota = int(resposta["nota"])
    except (KeyError, TypeError, ValueError) as e:
        raise IAErro("a triagem veio fora do formato esperado") from e
    return max(0, min(100, nota)), " ".join(str(resposta.get("motivo", "")).split())


def traduzir(textos: list[str], assunto: str, modelo: str | None) -> list[str]:
    """Legenda nos dois idiomas: o que está em português vai para inglês, e qualquer outro idioma para português."""
    linhas = "\n".join(f"[{n}] {t}" for n, t in enumerate(textos))
    prompt = f"""Estas são as linhas de legenda de um corte do vídeo "{assunto}", uma por linha, no formato [número] texto.
Elas vêm de transcrição automática e podem ter erros de reconhecimento; use o contexto para entender.

Traduza cada linha seguindo esta regra:
- linha em português: traduza para inglês;
- linha em qualquer outro idioma: traduza para português do Brasil.

Cada tradução é uma legenda: curta, natural, com no máximo 70 caracteres, sem aspas nem o número da linha.
Devolva exatamente {len(textos)} traduções, na mesma ordem das linhas.

{linhas}"""
    traducoes = _pedir(prompt, modelo, SISTEMA_TRADUCAO, ESQUEMA_TRADUCAO, "traducao").get("traducoes")
    if not isinstance(traducoes, list) or len(traducoes) != len(textos):
        raise IAErro("a tradução da legenda veio fora do formato esperado")
    return [" ".join(str(t).split()) for t in traducoes]


def _pedir(prompt: str, modelo: str | None, sistema: str, esquema: dict, tipo: str) -> dict:
    """`tipo` ("analise", "traducao", "triagem" ou "temas") serve para a conta de gastos separar as chamadas."""
    qual = backend()
    if qual == "api":
        return _via_api(prompt, modelo, sistema, esquema, tipo)
    if qual == "cli":
        return _via_cli(prompt, modelo, sistema, esquema, tipo)
    raise IAErro("nenhum acesso ao Claude encontrado (defina ANTHROPIC_API_KEY ou instale/logue o Claude Code)")


def _via_api(prompt: str, modelo: str | None, sistema: str, esquema: dict, tipo: str) -> dict:
    import anthropic

    modelo = modelo or MODELO_API
    pedido = dict(
        model=modelo,
        max_tokens=16000,
        system=sistema,
        output_config={"effort": ESFORCO_TRIAGEM if tipo == "triagem" else "medium",
                       "format": {"type": "json_schema", "schema": esquema}},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        cliente = anthropic.Anthropic(api_key=config("ANTHROPIC_API_KEY"))
        if modelo_barato(modelo):  # o Haiku não tem modelo alternativo para recusas: vai sem esse recurso
            resposta = cliente.messages.create(**pedido)
        else:
            # se o modelo principal recusar o pedido, a API refaz em um modelo alternativo
            resposta = cliente.beta.messages.create(**pedido, betas=["server-side-fallback-2026-07-01"],
                                                    fallbacks="default")
    except anthropic.AuthenticationError as e:
        raise IAErro("ANTHROPIC_API_KEY inválida") from e
    except anthropic.RateLimitError as e:
        raise IAErro("limite de requisições da API atingido") from e
    except anthropic.APIStatusError as e:
        raise IAErro(f"erro da API ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise IAErro("sem conexão com a API da Anthropic") from e

    uso = resposta.usage  # a chamada é cobrada mesmo que a resposta acabe recusada ou cortada
    gasto.ia(tipo, [resposta.model], uso.input_tokens or 0, uso.output_tokens or 0,
             getattr(uso, "cache_read_input_tokens", 0) or 0, getattr(uso, "cache_creation_input_tokens", 0) or 0)
    if resposta.stop_reason == "refusal":
        raise IAErro("a IA recusou analisar este trecho")
    if resposta.stop_reason == "max_tokens":
        raise IAErro("resposta da IA cortada por limite de tamanho")
    texto = next((b.text for b in resposta.content if b.type == "text"), "")
    return _ler_json(texto)


def _via_cli(prompt: str, modelo: str | None, sistema: str, esquema: dict, tipo: str) -> dict:
    cmd = [shutil.which("claude"), "-p", "--output-format", "json", "--tools", "", "--no-session-persistence",
           "--system-prompt", sistema, "--json-schema", json.dumps(esquema)]
    if modelo:
        cmd += ["--model", modelo]
    if tipo == "triagem":  # a análise e a tradução seguem no esforço padrão do Claude Code
        cmd += ["--effort", ESFORCO_TRIAGEM]

    env = dict(os.environ)
    if env.get("CLAUDECODE"):
        # rodando de dentro de uma sessão do Claude Code: não herdar a configuração dela
        env = {k: v for k, v in env.items() if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
    # Cada pedido leva uma transcrição diferente e nunca é repetido. Por padrão o Claude Code grava o pedido inteiro
    # em cache, o que custa o dobro da entrada normal e aqui nunca é reaproveitado; sem cache, a entrada sai pela metade.
    env["DISABLE_PROMPT_CACHING"] = "1"

    with tempfile.TemporaryDirectory() as pasta:  # pasta vazia: não carrega contexto de projeto
        try:
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", cwd=pasta, env=env, timeout=900)
        except subprocess.TimeoutExpired as e:
            raise IAErro("o Claude CLI demorou demais para responder") from e

    try:
        saida = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise IAErro(f"o Claude CLI falhou: {(r.stderr or r.stdout).strip()[-300:]}") from e
    uso = saida.get("usage") if isinstance(saida.get("usage"), dict) else {}
    if uso:  # o Claude Code devolve os tokens e o custo equivalente na API de cada chamada
        custo = saida.get("total_cost_usd")
        gasto.ia(tipo, sorted(saida.get("modelUsage") or {}), int(uso.get("input_tokens") or 0),
                 int(uso.get("output_tokens") or 0), int(uso.get("cache_read_input_tokens") or 0),
                 int(uso.get("cache_creation_input_tokens") or 0),
                 float(custo) if isinstance(custo, (int, float)) else None)
    if saida.get("is_error"):
        raise IAErro(str(saida.get("result") or "erro desconhecido do Claude CLI"))
    estruturado = saida.get("structured_output")
    if isinstance(estruturado, dict):
        return estruturado
    return _ler_json(str(saida.get("result") or ""))


def _ler_json(texto: str) -> dict:
    inicio, fim = texto.find("{"), texto.rfind("}")
    if inicio < 0 or fim <= inicio:
        raise IAErro("a IA não devolveu JSON")
    try:
        return json.loads(texto[inicio : fim + 1])
    except json.JSONDecodeError as e:
        raise IAErro("a IA devolveu JSON inválido") from e
