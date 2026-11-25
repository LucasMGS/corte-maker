"""Linha de comando: link ou arquivo -> cortes verticais prontos para postar."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from . import aovivo, editar, narrar, pipeline


def _segundos(texto: str) -> float:
    try:
        return pipeline.segundos(texto)
    except ValueError:
        raise argparse.ArgumentTypeError(f"tempo inválido: {texto!r} (use 90, 1:30 ou 1:02:03)")


def _argumentos() -> tuple[list[str], pipeline.Opcoes, bool]:
    p = argparse.ArgumentParser(
        prog="cortes",
        description="Gera cortes verticais (YouTube Shorts / TikTok) a partir de lives e vídeos.",
    )
    p.add_argument("origens", nargs="+", metavar="LINK_OU_ARQUIVO", help="link (YouTube, Twitch, Kick...) ou arquivo de vídeo")
    p.add_argument("-n", "--cortes", type=int, default=5, help="quantos cortes gerar por vídeo (padrão: 5)")
    p.add_argument("--min", type=int, default=20, dest="dmin", help="duração mínima de cada corte, em segundos (padrão: 20)")
    p.add_argument("--max", type=int, default=60, dest="dmax", help="duração máxima de cada corte, em segundos (padrão: 60)")
    p.add_argument("--layout", choices=editar.LAYOUTS, default="blur",
                   help="blur: vídeo aproximado sobre fundo desfocado (padrão) | cheio: tela cheia cortando as laterais | "
                        "inteiro: vídeo completo sobre fundo desfocado | preto: vídeo completo sobre fundo preto | "
                        "camera: câmera do streamer em cima e o jogo embaixo (precisa de --camera)")
    p.add_argument("--camera", default="", metavar="X,Y,L,A",
                   help='(--layout camera) onde fica a câmera no vídeo, em frações do quadro: x,y,largura,altura; '
                        'ex.: "0.75,0.7,0.25,0.3" para o canto inferior direito')
    p.add_argument("--contexto", default="", help='dica para a IA, ex.: "gameplay de GTA 6, streamer fulano, foco em humor"')
    p.add_argument("--inicio", type=_segundos, help="(links) baixa a partir deste ponto, ex.: 1:30:00")
    p.add_argument("--fim", type=_segundos, help="(links) baixa só até este ponto")
    p.add_argument("--idioma", default="auto", help="idioma falado, ex.: pt, en (padrão: detectar)")
    p.add_argument("--whisper", default="large-v3-turbo", help="modelo de transcrição (padrão: large-v3-turbo)")
    p.add_argument("--modelo", default=None, help="modelo do Claude para a análise (padrão: o da sua conta)")
    p.add_argument("--sem-ia", action="store_true", help="não usa o Claude; escolhe os momentos só pelos picos de áudio")
    p.add_argument("--sem-legenda", action="store_true", help="não queima legenda no vídeo")
    p.add_argument("--marca", default=pipeline.MARCA,
                   help=f'assinatura do canal no topo de cada corte (padrão: {pipeline.MARCA}; use --marca "" para tirar)')
    p.add_argument("--comentario", choices=pipeline.COMENTARIOS, default="pt",
                   help="idioma do comentário que a IA escreve e uma voz narra no fim de cada corte, com o vídeo "
                        "parado (padrão: pt; nao: sem comentário)")
    p.add_argument("--voz", choices=sorted(narrar.vozes()), default="",
                   help=f"voz do narrador (padrão: {narrar.padrao()}); as amostras ficam em voz\\amostras")
    p.add_argument("--abertura", action="store_true", help="o narrador também fala antes do corte, e não só no fim")
    p.add_argument("--narrar-na-hora", action="store_true",
                   help="grava a voz do narrador em todo corte já na geração; o padrão é a IA só escrever o "
                        "comentário e a voz ser gravada quando você pedir, corte a corte, na interface")
    p.add_argument("--sem-traducao", action="store_true",
                   help="legenda só no idioma falado; o padrão é ter embaixo uma segunda linha traduzida "
                        "(português para inglês, outros idiomas para português)")
    p.add_argument("--manter-pausas", action="store_true",
                   help="não encurta os silêncios do corte; o padrão é tirar o silêncio das pontas e encurtar as "
                        "pausas mudas de 1 s ou mais, para o ritmo não cair")
    p.add_argument("--baixar-tudo", action="store_true",
                   help="(links) baixa o vídeo inteiro; o padrão é baixar só o áudio e os trechos que viram corte")
    p.add_argument("--saida", type=Path, default=pipeline.RAIZ / "saida", help="pasta dos cortes prontos (padrão: saida/)")
    vivo = p.add_argument_group("modo ao vivo")
    vivo.add_argument("--ao-vivo", action="store_true",
                      help="acompanha uma live que está no ar e gera cortes enquanto ela acontece (Ctrl+C para parar)")
    vivo.add_argument("--limiar", type=int, default=75, help="nota mínima (0 a 100) para um momento virar corte (padrão: 75)")
    vivo.add_argument("--intervalo", type=int, default=150, help="segundos de live entre uma análise e outra (padrão: 150)")
    vivo.add_argument("--triagem", action="store_true",
                      help="um modelo barato lê cada trecho antes, e a análise completa só roda quando ele vê chance de "
                           "corte: gasta cerca de metade da IA, mas pode deixar passar algum momento bom")
    a = vars(p.parse_args())
    origens, ao_vivo = a.pop("origens"), a.pop("ao_vivo")
    opcoes = pipeline.Opcoes(**a)
    try:
        opcoes.validar()
    except ValueError as erro:
        p.error(str(erro))
    if ao_vivo and len(origens) != 1:
        p.error("o modo ao vivo acompanha uma live por vez")
    return origens, opcoes, ao_vivo


def _acompanhar(url: str, opcoes: pipeline.Opcoes) -> None:
    """Roda o monitor numa thread para o Ctrl+C virar um pedido de parada, e não um corte pela metade."""
    parar = threading.Event()
    erro: list[BaseException] = []

    def alvo() -> None:
        try:
            aovivo.monitorar(url, opcoes, parar)
        except Exception as e:
            erro.append(e)

    t = threading.Thread(target=alvo, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        print("\nParando: fechando a gravação e analisando o que faltou...", flush=True)
        parar.set()
        t.join()
    if erro:
        raise erro[0]


def main() -> None:
    for fluxo in (sys.stdout, sys.stderr):
        fluxo.reconfigure(encoding="utf-8", errors="replace")
    origens, opcoes, ao_vivo = _argumentos()
    if ao_vivo:
        print(f"\n=== AO VIVO: {origens[0]}")
        try:
            _acompanhar(origens[0], opcoes)
        except Exception as erro:
            sys.exit(f"ERRO: {erro}")
        return
    falhas = 0
    for origem in origens:
        print(f"\n=== {origem}")
        try:
            pipeline.processar(origem, opcoes)
        except KeyboardInterrupt:
            print("\nInterrompido.")
            sys.exit(130)
        except Exception as erro:
            falhas += 1
            print(f"ERRO em {origem}: {erro}", file=sys.stderr)
    sys.exit(1 if falhas else 0)
