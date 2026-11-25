"""Gera o arquivo .ass do corte: título fixo + legenda palavra a palavra com destaque."""

from __future__ import annotations

from pathlib import Path

FONTE = "Arial Black"
COR_DESTAQUE = "&H00FFFF&"  # amarelo (ASS usa BGR)
COR_NORMAL = "&HFFFFFF&"
COR_NARRADOR = "&HFF9BC4&"  # lilás: destaque das palavras do narrador, para não confundir com a fala do vídeo
Y_MARCA = 170  # abaixo da barra superior dos apps e acima do título em todos os enquadramentos
GANCHO_S = 2.5  # segundos em que o gancho fica na tela, no lugar do título, logo que o corte começa

CABECALHO = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Legenda,{FONTE},84,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,7,3,5,60,60,60,1
Style: Titulo,{FONTE},58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,6,2,2,70,70,60,1
Style: Gancho,{FONTE},74,&H0000FFFF,&H0000FFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,7,3,2,70,70,60,1
Style: Traducao,{FONTE},42,&H00F2F2F2,&H00F2F2F2,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,4,1,8,70,70,60,1
Style: Portugues,{FONTE},66,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,7,3,2,60,60,60,1
Style: Marca,{FONTE},34,&H40FFFFFF,&H40FFFFFF,&H80000000,&H96000000,0,0,0,0,100,100,1,0,1,3,0,8,60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# Compilação (vídeo horizontal 1920x1080): cartela entre os momentos, rótulo sobre cada um e texto da miniatura.
CABECALHO_H = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Numero,{FONTE},250,&H0000FFFF,&H0000FFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,10,5,2,200,200,60,1
Style: Momento,{FONTE},78,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,7,3,8,220,220,60,1
Style: Credito,{FONTE},36,&H30FFFFFF,&H30FFFFFF,&H40000000,&H96000000,0,0,0,0,100,100,1,0,1,3,0,2,200,200,60,1
Style: Rotulo,{FONTE},46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,1,5,2,7,48,700,40,1
Style: Assinatura,{FONTE},28,&H50FFFFFF,&H50FFFFFF,&H80000000,&H96000000,0,0,0,0,100,100,1,0,1,3,0,7,48,700,40,1
Style: Capa,{FONTE},200,&H0000FFFF,&H0000FFFF,&H00000000,&H78000000,0,0,0,0,100,100,0,0,1,16,8,1,70,520,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def cartela(caminho: Path, numero: int, titulo: str, credito: str, duracao: float) -> None:
    """Cartela entre dois momentos da compilação: o número grande, o título do momento e de quem é o vídeo."""
    fim = _tempo(duracao)
    eventos = [
        f"Dialogue: 1,{_tempo(0)},{fim},Numero,,0,0,0,,"
        f"{{\\an2\\pos(960,500)\\fscx70\\fscy70\\t(0,140,\\fscx100\\fscy100)\\fad(80,0)}}#{numero}",
        f"Dialogue: 1,{_tempo(0)},{fim},Momento,,0,0,0,,{{\\an8\\pos(960,540)\\fad(160,0)}}{_limpo(titulo)}",
    ]
    if credito:
        eventos.append(f"Dialogue: 0,{_tempo(0)},{fim},Credito,,0,0,0,,{{\\an2\\pos(960,1010)\\fad(160,0)}}{_limpo(credito)}")
    caminho.write_text(CABECALHO_H + "\n".join(eventos) + "\n", encoding="utf-8-sig")


def rotulo(caminho: Path, numero: int, titulo: str, assinatura: str, duracao: float, ate: float = 5.0) -> None:
    """O que vai escrito sobre cada momento da compilação: no canto de cima, o número e o título nos primeiros
    segundos e, o momento inteiro, a `assinatura` discreta (marca do canal de cortes e crédito de quem é o vídeo)."""
    eventos = []
    y = 34
    if assinatura:
        eventos.append(f"Dialogue: 0,{_tempo(0)},{_tempo(duracao)},Assinatura,,0,0,0,,{{\\an7\\pos(48,{y})}}{_limpo(assinatura)}")
        y += 44
    if titulo and duracao > ate + 1:
        eventos.append(f"Dialogue: 1,{_tempo(0)},{_tempo(ate)},Rotulo,,0,0,0,,"
                       f"{{\\an7\\pos(48,{y})\\fad(150,350)}}{{\\c{COR_DESTAQUE}}}#{numero}{{\\c{COR_NORMAL}}}  {_limpo(titulo)}")
    caminho.write_text(CABECALHO_H + "\n".join(eventos) + "\n", encoding="utf-8-sig")


def capa(caminho: Path, texto: str) -> None:
    """Texto da miniatura da compilação: poucas palavras, grandes, no canto de baixo, para o quadro continuar
    sendo o ponto focal. O tamanho diminui quando o texto é mais comprido."""
    texto = _limpo(texto).strip().upper()
    tamanho = 200 if len(texto) <= 9 else 160 if len(texto) <= 16 else 135 if len(texto) <= 24 else 115
    caminho.write_text(CABECALHO_H + f"Dialogue: 0,{_tempo(0)},{_tempo(10)},Capa,,0,0,0,,"
                       f"{{\\an1\\pos(70,1020)\\fs{tamanho}}}{texto}\n", encoding="utf-8-sig")


def _tempo(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs % 360000 // 6000:02d}:{cs % 6000 // 100:02d}.{cs % 100:02d}"


def _limpo(texto: str) -> str:
    return texto.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


# Palavras que denunciam o idioma de uma frase curta de legenda ("a", "e" e "no" existem nos dois e ficam de fora).
_PT = set("""de que não nao é o os as um uma pra para você voce vocês eu isso isto tá ta mano cara com na nos se mas
muito aqui ele ela meu minha foi vai tem está esta estou agora porque também tambem só então entao já quando como do
da dos das em ao né mais bem nada tudo aí ai olha mesmo pode vamo vamos tô to era ser fazer sei qual quem onde""".split())
_EN = set("""the you i is it to and what that this of in my me we he she they was are be do don't not yes so just
like oh go get got on for with your all can what's it's i'm that's right now here there look come man bro dude
gonna wanna yeah okay why how who where have has had did will would""".split())


def _portugues(texto: str) -> int:
    """Quanto a frase parece português (positivo) ou inglês (negativo)."""
    palavras = [p.strip(".,;:!?…\"'()").lower() for p in texto.split()]
    acentos = sum(texto.lower().count(c) for c in "ãõçêâôáéíóú")
    return sum(p in _PT for p in palavras) + min(acentos, 3) - sum(p in _EN for p in palavras)


def _grupos(palavras: list[dict]) -> list[list[dict]]:
    """Blocos de até 3 palavras / ~16 letras, quebrando também em pausa ou fim de frase."""
    grupos: list[list[dict]] = []
    atual: list[dict] = []
    for p in palavras:
        if atual:
            letras = sum(len(x["t"]) for x in atual) + len(atual)
            pausa = p["i"] - atual[-1]["f"]
            troca = p.get("cor") != atual[-1].get("cor")  # narrador e fala do vídeo nunca dividem um bloco
            if len(atual) >= 3 or letras + len(p["t"]) > 16 or pausa > 0.45 or atual[-1]["t"][-1] in ".?!…," or troca:
                grupos.append(atual)
                atual = []
        atual.append(p)
    if atual:
        grupos.append(atual)
    return grupos


def gerar(caminho: Path, palavras: list[dict], titulo: str, duracao: float, y_legenda: int,
          titulo_pos: tuple[int, int, int], titulo_ate: float, marca: str = "",
          traducoes: list[dict] | None = None, gancho: str = "") -> None:
    """`palavras` com tempos relativos ao início do corte; uma palavra pode trazer `cor` para o seu destaque.
    `titulo_pos` = (alinhamento ASS, x, y).
    `marca` é a assinatura do canal, discreta e fixa no topo o corte inteiro.
    `traducoes` são frases inteiras (i, f, t) no outro idioma, mostradas menores logo abaixo da legenda.
    `gancho` é a frase que abre o corte: fica grande no lugar do título por GANCHO_S segundos, e o título vem depois."""
    eventos: list[str] = []

    if traducoes:
        y_legenda -= 45  # a legenda principal sobe um pouco para caber a linha traduzida embaixo
        # O público é brasileiro: o português é sempre a linha grande. Quando a fala é em outro idioma e a tradução
        # é que está em português, as duas trocam de lugar naquela frase: o português vai grande em cima e a fala
        # original, pequena embaixo.
        trocadas: list[tuple[float, float]] = []
        for n, tr in enumerate(traducoes):
            ate = min(tr["f"] + 0.4, traducoes[n + 1]["i"]) if n + 1 < len(traducoes) else tr["f"] + 0.4
            if ate <= tr["i"]:
                continue
            quando = f"{_tempo(tr['i'])},{_tempo(min(ate, duracao))}"
            dentro = [p for p in palavras if p["i"] >= tr["i"] - 0.05 and p["f"] <= tr["f"] + 0.05]
            original = " ".join(p["t"] for p in dentro)
            if dentro and _portugues(tr["t"]) > _portugues(original):
                trocadas.append((tr["i"] - 0.05, tr["f"] + 0.05))
                eventos.append(f"Dialogue: 0,{quando},Portugues,,0,0,0,,"
                               f"{{\\an2\\pos(540,{y_legenda + 48})\\fscx92\\fscy92\\t(0,90,\\fscx100\\fscy100)}}"
                               f"{_limpo(tr['t']).upper()}")
                eventos.append(f"Dialogue: 0,{quando},Traducao,,0,0,0,,{{\\pos(540,{y_legenda + 62})}}{_limpo(original)}")
            else:
                eventos.append(f"Dialogue: 0,{quando},Traducao,,0,0,0,,{{\\pos(540,{y_legenda + 62})}}{_limpo(tr['t'])}")
        palavras = [p for p in palavras if not any(a <= p["i"] and p["f"] <= b for a, b in trocadas)]

    if marca:
        eventos.append(f"Dialogue: 2,{_tempo(0)},{_tempo(duracao)},Marca,,0,0,0,,"
                       f"{{\\pos(540,{Y_MARCA})}}{_limpo(marca)}")

    alinh, x, y = titulo_pos
    comeco_titulo = 0.0
    gancho = _limpo(gancho).strip()
    if gancho and duracao > GANCHO_S + 1:
        comeco_titulo = GANCHO_S
        eventos.append(f"Dialogue: 1,{_tempo(0)},{_tempo(GANCHO_S)},Gancho,,0,0,0,,"
                       f"{{\\an{alinh}\\pos({x},{y})\\fscx70\\fscy70\\t(0,120,\\fscx100\\fscy100)\\fad(0,150)}}"
                       f"{gancho.upper()}")
    if titulo:
        # com gancho, o título entra depois dele e fica na tela pelo mesmo tempo que ficaria sem
        eventos.append(f"Dialogue: 1,{_tempo(comeco_titulo)},{_tempo(min(titulo_ate + comeco_titulo, duracao))},"
                       f"Titulo,,0,0,0,,{{\\an{alinh}\\pos({x},{y})}}{_limpo(titulo)}")

    grupos = _grupos(palavras)
    for g, grupo in enumerate(grupos):
        limite = grupos[g + 1][0]["i"] if g + 1 < len(grupos) else duracao
        textos = [_limpo(p["t"]).upper().rstrip(".,;:") or _limpo(p["t"]) for p in grupo]
        for k, p in enumerate(grupo):
            ini = p["i"]
            fim = grupo[k + 1]["i"] if k + 1 < len(grupo) else min(max(p["f"] + 0.25, ini + 0.2), limite)
            if fim <= ini:
                continue
            cor = p.get("cor", COR_DESTAQUE)
            partes = [f"{{\\c{cor}}}{t}{{\\c{COR_NORMAL}}}" if j == k else t for j, t in enumerate(textos)]
            entrada = "\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)" if k == 0 else ""
            eventos.append(f"Dialogue: 0,{_tempo(ini)},{_tempo(fim)},Legenda,,0,0,0,,"
                           f"{{\\pos(540,{y_legenda}){entrada}}}{' '.join(partes)}")

    caminho.write_text(CABECALHO + "\n".join(eventos) + "\n", encoding="utf-8-sig")
