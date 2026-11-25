"""Escolha dos melhores momentos: IA sobre a transcrição, com picos de áudio como sinal e como plano B."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

import numpy as np

from . import gasto, ia, progresso
from .audio import PASSO
from .narrar import IDIOMAS

BLOCO = 20 * 60  # segundos de transcrição por chamada à IA
SOBREPOSICAO = 2 * 60
LIMIAR_PICO = 2.0  # z de volume a partir do qual a linha ganha a marca de pico
# Modo ao vivo: a triagem barata descarta o trecho quando a nota dela fica mais do que isto abaixo da nota mínima.
# A folga é larga porque o modelo barato dá notas bem mais baixas que a análise completa: num teste com 100 janelas,
# os momentos que a análise completa pontuou de 84 a 88 receberam da triagem entre 45 e 68 (e um deles, 30 a 40).
FOLGA_TRIAGEM = 30


@dataclass
class Linha:
    n: int
    inicio: float
    fim: float
    texto: str
    pico: bool = False


@dataclass
class Momento:
    inicio: float
    fim: float
    nota: int
    titulo: str
    descricao: str = ""
    hashtags: list[str] = field(default_factory=list)
    motivo: str = ""
    origem: str = "ia"  # "ia", "audio" ou "manual" (trecho escolhido à mão numa live)
    abertura: str = ""  # comentário do narrador falado antes do corte
    fecho: str = ""  # comentário do narrador falado depois do corte
    gancho: str = ""  # frase curta que aparece grande na tela nos primeiros segundos, para segurar quem está passando

    def como_dict(self) -> dict:
        return asdict(self)


def hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def montar_linhas(palavras: list[dict], energia: np.ndarray) -> list[Linha]:
    """Agrupa as palavras em falas curtas (quebra em pontuação final, pausa ou tamanho)."""
    linhas: list[Linha] = []
    atual: list[dict] = []

    def fechar() -> None:
        if not atual:
            return
        ini, fim = atual[0]["i"], atual[-1]["f"]
        a, b = int(ini / PASSO), max(int(fim / PASSO), int(ini / PASSO) + 1)
        pico = bool(energia[a:b].max() >= LIMIAR_PICO) if a < len(energia) else False
        linhas.append(Linha(len(linhas), ini, fim, " ".join(p["t"] for p in atual), pico))
        atual.clear()

    for p in palavras:
        if atual:
            pausa = p["i"] - atual[-1]["f"]
            duracao = atual[-1]["f"] - atual[0]["i"]
            fim_de_frase = atual[-1]["t"][-1] in ".?!…"
            if pausa > 0.8 or duracao > 10 or (fim_de_frase and duracao > 1.5):
                fechar()
        atual.append(p)
    fechar()
    return linhas


def _cabecalho(titulo: str, canal: str, contexto: str) -> str:
    cabecalho = f'Vídeo: "{titulo}"' + (f" — canal: {canal}" if canal else "")
    if contexto:
        cabecalho += f"\nContexto informado pelo dono do canal de cortes: {contexto}"
    return cabecalho


def _transcricao(linhas: list[Linha]) -> str:
    return "\n".join(f"[{l.n}] {hms(l.inicio)} {'🔊 ' if l.pico else ''}{l.texto}" for l in linhas)


def triar(linhas: list[Linha], titulo: str, canal: str, contexto: str, dmin: int, dmax: int) -> tuple[int, str]:
    """Triagem do modo ao vivo: o modelo barato lê o trecho e dá a nota do melhor momento que há nele.
    Quem chama decide, pela nota, se vale gastar a análise completa. Devolve (nota, motivo)."""
    return ia.triar(f"""{_cabecalho(titulo, canal, contexto)}

Abaixo está a transcrição de um trecho do vídeo, uma fala por linha, no formato:
[número] HH:MM:SS texto
Linhas marcadas com 🔊 têm pico de volume (grito, risada, reação forte, explosão). A transcrição é automática e pode ter erros de reconhecimento.

Um editor procura aqui cortes independentes de {dmin} a {dmax} segundos. Antes de ele analisar o trecho com calma, diga só se vale a pena: qual é a nota, de 0 a 100, do MELHOR momento deste trecho como corte?

Um bom corte:
- prende nos primeiros 3 segundos: começa já na ação ou numa frase forte, sem cumprimento nem enrolação;
- é entendido sozinho, sem precisar do resto do vídeo;
- tem começo e desfecho: a piada completa, o fail e a reação, a jogada e o resultado, a opinião e o argumento.

Costumam funcionar: reações fortes, momentos engraçados, fails, jogadas impressionantes, bugs, descobertas e novidades do jogo, opiniões polêmicas, histórias curtas.

Escala da nota, potencial de engajamento: 90 ou mais só para momentos excepcionais; 75 a 89 para um corte bom, que vale publicar; 50 a 74 para algo morno; abaixo de 50 quando é conversa comum, espera, leitura de chat ou nada acontece.
Uma nota alta só faz o trecho passar por uma segunda avaliação, mais cuidadosa; uma nota baixa descarta o trecho de vez. Na dúvida entre duas notas, fique com a mais alta.

Devolva:
- nota: a nota do melhor momento do trecho.
- motivo: até 15 palavras dizendo qual é esse momento, ou que não há nenhum.

Transcrição:
{_transcricao(linhas)}""")


def _prompt(linhas: list[Linha], titulo: str, canal: str, contexto: str, k: int, dmin: int, dmax: int,
            comentario: str = "", abertura: bool = False) -> str:
    cabecalho = _cabecalho(titulo, canal, contexto)
    if comentario:
        lingua = IDIOMAS[comentario]
        idioma_textos = f"em {lingua}, mesmo que o vídeo esteja em outro idioma"
        antes = (f"- abertura: comentário de um narrador, falado ANTES de o corte começar, em {lingua}, com 8 a 14 palavras. "
                 "Dá o contexto que quem caiu de paraquedas não tem (quem é, o que está em jogo) e cria expectativa, "
                 "sem entregar o desfecho." if abertura else '- abertura: deixe como texto vazio ("").')
        narrador = f"""{antes}
- fecho: comentário de um narrador, falado DEPOIS do corte, em {lingua}, com 14 a 22 palavras, em duas frases. A primeira é uma opinião, análise ou consequência do que aconteceu, nunca um resumo. A segunda é uma pergunta direta ao público sobre o que ele acabou de ver, que dê vontade de responder, terminando com um convite curto para comentar.
O comentário será lido por uma voz sintética: escreva como se fala, com energia, em frases simples, sem emojis, hashtags, aspas, parênteses nem siglas difíceis de pronunciar. Só afirme o que a transcrição sustenta."""
    else:
        idioma_textos = "no idioma do vídeo"
        narrador = '- abertura e fecho: deixe os dois como texto vazio ("").'
    transcricao = _transcricao(linhas)
    return f"""{cabecalho}

Abaixo está a transcrição de um trecho do vídeo, uma fala por linha, no formato:
[número] HH:MM:SS texto
Linhas marcadas com 🔊 têm pico de volume (grito, risada, reação forte, explosão). A transcrição é automática e pode ter erros de reconhecimento.

Escolha até {k} momentos que funcionem como cortes independentes de {dmin} a {dmax} segundos.

Um bom corte:
- prende nos primeiros 2 segundos: começa já na ação ou numa frase forte, o mais perto possível do ponto alto, sem cumprimento nem enrolação;
- é entendido sozinho, sem precisar do resto do vídeo;
- tem começo e desfecho: a piada completa, o fail e a reação, a jogada e o resultado, a opinião e o argumento;
- tem ritmo: não carrega no meio trechos longos de espera, silêncio ou tela de carregamento (os horários das linhas mostram onde há buracos na fala);
- termina logo depois do ponto alto, numa frase concluída.

Costumam funcionar: reações fortes, momentos engraçados, fails, jogadas impressionantes, bugs, descobertas e novidades do jogo, opiniões polêmicas, histórias curtas.

Para cada momento devolva:
- linha_inicio e linha_fim: números das linhas (inclusive) em que o corte começa e termina. Confira pelos horários que a duração fica entre {dmin} e {dmax} segundos.
- nota: de 0 a 100, potencial de engajamento. Seja exigente: 90 ou mais só para momentos excepcionais.
- titulo: até 60 caracteres, {idioma_textos}, direto e do jeito que a comunidade do jogo fala. Destaque a surpresa ou a piada sem entregar o desfecho e sem prometer o que o corte não entrega.
- gancho: texto que aparece grande na tela nos 2 primeiros segundos do corte, {idioma_textos}, com até 7 palavras. Uma pergunta ou provocação que dê vontade de ver o desfecho sem entregá-lo, diferente do título, sem emojis nem hashtags.
- descricao: uma ou duas frases para a legenda do post, {idioma_textos}.
- hashtags: 4 a 6, sem o #, em minúsculas, misturando gerais e específicas do conteúdo.
- motivo: uma frase dizendo por que o momento funciona.
{narrador}

Não force: se o trecho não tiver nada que valha um corte, devolva a lista vazia. Os momentos não podem se sobrepor.

Transcrição:
{transcricao}"""


def _blocos(linhas: list[Linha]) -> list[list[Linha]]:
    if not linhas:
        return []
    fim_total = linhas[-1].fim
    if fim_total - linhas[0].inicio <= BLOCO * 1.3:
        return [linhas]
    blocos, t = [], linhas[0].inicio
    while t < fim_total:
        bloco = [l for l in linhas if t <= l.inicio < t + BLOCO + SOBREPOSICAO]
        if bloco:
            blocos.append(bloco)
        t += BLOCO
    return blocos


def _ajustar(linhas: list[Linha], a: int, b: int, dmin: int, dmax: int) -> tuple[int, int] | None:
    """Garante índices válidos e duração dentro dos limites, mexendo só no fim do corte."""
    if not (0 <= a <= b < len(linhas)):
        return None
    while b > a and linhas[b].fim - linhas[a].inicio > dmax:
        b -= 1
    while b + 1 < len(linhas) and linhas[b].fim - linhas[a].inicio < dmin \
            and linhas[b + 1].fim - linhas[a].inicio <= dmax:
        b += 1
    duracao = linhas[b].fim - linhas[a].inicio
    return (a, b) if dmin * 0.6 <= duracao <= dmax + 1 else None


def por_ia(linhas: list[Linha], titulo: str, canal: str, contexto: str, n: int, dmin: int, dmax: int,
           modelo: str | None, comentario: str = "", abertura: bool = False) -> list[Momento]:
    """`comentario` é o idioma ("pt" ou "en") em que a IA escreve a narração de cada corte; vazio, não escreve.
    Com `abertura`, o narrador também fala antes do corte, e não só depois."""
    blocos = _blocos(linhas)
    if not blocos:
        return []
    k = n + 2 if len(blocos) == 1 else min(6, max(3, n))

    conta = gasto.atual()  # os blocos seguintes rodam em threads auxiliares, que somam na conta deste trabalho

    def analisar(bloco: list[Linha]) -> list[Momento]:
        gasto.usar(conta)
        bruto = ia.pedir_momentos(_prompt(bloco, titulo, canal, contexto, k, dmin, dmax, comentario, abertura), modelo)
        achados = []
        for m in bruto:
            try:
                par = _ajustar(linhas, int(m["linha_inicio"]), int(m["linha_fim"]), dmin, dmax)
            except (KeyError, TypeError, ValueError):
                continue
            if not par:
                continue
            a, b = par
            achados.append(Momento(
                inicio=max(0.0, linhas[a].inicio - 0.25),
                fim=linhas[b].fim + 0.35,
                nota=max(0, min(100, int(m.get("nota", 50)))),
                titulo=str(m.get("titulo", "")).strip() or titulo,
                descricao=str(m.get("descricao", "")).strip(),
                hashtags=[str(h).strip().lstrip("#").replace(" ", "") for h in m.get("hashtags", []) if str(h).strip()],
                motivo=str(m.get("motivo", "")).strip(),
                abertura=str(m.get("abertura", "")).strip() if comentario and abertura else "",
                fecho=str(m.get("fecho", "")).strip() if comentario else "",
                gancho=" ".join(str(m.get("gancho", "")).split()),
            ))
        return achados

    # o primeiro bloco roda sozinho: se a IA estiver sem acesso, falha logo em vez de N vezes
    progresso.info(f"A IA está lendo a transcrição ({len(blocos)} bloco(s) de até {BLOCO // 60} min)...")
    encontrados = analisar(blocos[0])
    progresso.pct(100 / len(blocos))
    if len(blocos) > 1:
        with ThreadPoolExecutor(max_workers=3) as pool:
            for i, achados in enumerate(pool.map(analisar, blocos[1:]), start=2):
                encontrados += achados
                progresso.pct(100 * i / len(blocos))
                progresso.info(f"Bloco {i}/{len(blocos)} analisado")
    return _melhores(encontrados, n)


def descrever(linhas: list[Linha], titulo: str, canal: str, contexto: str, duracao: float, modelo: str | None,
              comentario: str = "", abertura: bool = False) -> Momento | None:
    """Um corte que o dono do canal escolheu à mão: a IA não escolhe o trecho, só escreve título, descrição,
    hashtags e a narração. Devolve um momento cobrindo o trecho inteiro, ou None se não houver fala para ler."""
    if not linhas:
        return None
    pedido = _prompt(linhas, titulo, canal, contexto, 1, 1, max(2, int(duracao) + 1), comentario, abertura) + (
        f"\n\nATENÇÃO: este trecho inteiro já foi escolhido à mão pelo dono do canal para virar um corte. "
        f"Devolva exatamente um momento, com linha_inicio {linhas[0].n} e linha_fim {linhas[-1].n}. "
        f"Não devolva a lista vazia. A nota continua sendo a sua avaliação sincera do trecho.")
    bruto = ia.pedir_momentos(pedido, modelo)
    if not bruto:
        return None
    m = bruto[0]
    return Momento(
        inicio=0.0, fim=duracao, nota=max(0, min(100, int(m.get("nota", 50) or 0))),
        titulo=str(m.get("titulo", "")).strip() or titulo, descricao=str(m.get("descricao", "")).strip(),
        hashtags=[str(h).strip().lstrip("#").replace(" ", "") for h in m.get("hashtags", []) if str(h).strip()],
        motivo=str(m.get("motivo", "")).strip(), origem="manual",
        abertura=str(m.get("abertura", "")).strip() if comentario and abertura else "",
        fecho=str(m.get("fecho", "")).strip() if comentario else "",
        gancho=" ".join(str(m.get("gancho", "")).split()),
    )


def por_audio(energia: np.ndarray, linhas: list[Linha], titulo: str, n: int, dmin: int, dmax: int,
              duracao_video: float, absoluta: bool = False) -> list[Momento]:
    """Plano B sem IA: janelas com mais volume acima do normal (gritos, risadas, ação).

    Por padrão a nota é relativa ao melhor trecho do próprio vídeo (o mais alto leva ~90). Com `absoluta`,
    ela depende só de quão acima do normal o trecho ficou, para o modo ao vivo poder exigir uma nota mínima.
    """
    janela = min(float(dmax), max(float(dmin), 35.0))
    if duracao_video <= janela + 2:
        return [Momento(0.0, duracao_video, 50, titulo, origem="audio")]
    w = int(janela / PASSO)
    if len(energia) <= w:
        return []
    empolgacao = np.convolve(np.clip(energia, 0, 6), np.ones(w, dtype=np.float32) / w, mode="valid")
    teto = float(empolgacao.max()) or 1.0

    momentos: list[Momento] = []
    restante = empolgacao.copy()
    while len(momentos) < n:
        i = int(restante.argmax())
        if not math.isfinite(restante[i]) or restante[i] <= 0:
            break
        ini, fim, alvo = i * PASSO, i * PASSO + janela, janela
        # aperta a janela em volta do trecho barulhento: ~2 s de preparação antes e ~3 s de reação depois
        altos = np.flatnonzero(energia[i : i + w] >= 1.0)
        if len(altos):
            ini = max(ini, (i + altos[0]) * PASSO - 2)
            fim = max(min(fim, (i + altos[-1]) * PASSO + 3), ini + dmin)
            alvo = fim - ini
        # encaixa o começo e o fim em falas inteiras, se houver alguma a até 3 s
        ini = min((l.inicio for l in linhas if abs(l.inicio - ini) <= 3), key=lambda t: abs(t - ini), default=ini)
        fim = min((l.fim for l in linhas if abs(l.fim - (ini + alvo)) <= 3), key=lambda t: abs(t - (ini + alvo)),
                  default=ini + alvo)
        if absoluta:
            trecho = np.clip(energia[int(ini / PASSO) : max(int(fim / PASSO), int(ini / PASSO) + 1)], 0, 6)
            nota = int(min(95, 35 + 30 * float(trecho.mean())))
        else:
            nota = int(40 + 50 * float(restante[i]) / teto)
        momentos.append(Momento(max(0.0, ini - 0.25), min(duracao_video, fim + 0.35), nota,
                                f"{titulo[:50]} #{len(momentos) + 1}", origem="audio",
                                motivo="Pico de volume acima do normal do vídeo."))
        restante[max(0, i - w) : i + w] = -np.inf
    return _melhores(momentos, n)


def _melhores(momentos: list[Momento], n: int) -> list[Momento]:
    """Ordena por nota e descarta os que se sobrepõem a um melhor."""
    escolhidos: list[Momento] = []
    for m in sorted(momentos, key=lambda m: m.nota, reverse=True):
        if all(min(m.fim, e.fim) - max(m.inicio, e.inicio) <= 1.0 for e in escolhidos):
            escolhidos.append(m)
        if len(escolhidos) == n:
            break
    return escolhidos
