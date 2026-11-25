"""Refaz um corte pronto a partir do trecho original guardado em `_fontes/`: outro enquadramento, com ou sem a
narração. A voz (paga) é gravada só na primeira vez em que se pede a narração de um corte e fica guardada; trocar o
enquadramento depois não grava de novo."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import editar, ffmpeg, ia, narrar, pipeline

_uma_de_cada_vez = threading.Lock()  # a montagem escreve arquivos temporários de nome fixo na pasta `_fontes`
_pedidos: dict[tuple[str, str], threading.Event] = {}  # corte sendo refeito -> o pedido de cancelar


def cancelar(pasta: Path, arquivo: str) -> bool:
    """Pede para parar a remontagem do corte. O vídeo que já existia fica como estava."""
    evento = _pedidos.get((str(pasta), arquivo))
    if evento is None:
        return False
    evento.set()
    return True


class RefazerErro(RuntimeError):
    pass


def _gravar_fala(texto: str, idioma: str, destino: Path, voz: str) -> narrar.Narrado:
    """Grava a fala com a voz pedida. Diferente da geração automática, não troca uma voz da ElevenLabs pela local em
    silêncio: quem pediu a narração de um corte quer aquela voz, então o erro (créditos acabaram, por exemplo) sobe."""
    escolhida = narrar.vozes().get(voz) or narrar.vozes()[narrar.padrao()]
    limpo = " ".join(texto.split())
    if escolhida.motor == "elevenlabs":
        if not narrar.tem_eleven():
            raise narrar.NarracaoErro("a voz escolhida é da ElevenLabs, mas não há ELEVENLABS_API_KEY no .env")
        return narrar._eleven(limpo, destino.with_suffix(".mp3"), escolhida.ref)
    return narrar._piper(limpo, idioma, destino.with_suffix(".wav"), escolhida)


def _trocar(novo: Path, destino: Path) -> None:
    """Põe o vídeo novo no lugar do antigo. No Windows, a troca falha enquanto o navegador está lendo o arquivo."""
    for _ in range(40):
        try:
            novo.replace(destino)
            return
        except PermissionError:
            time.sleep(0.25)
    novo.unlink(missing_ok=True)
    raise RefazerErro("o vídeo está aberto em outro programa; feche-o e tente de novo")


def _ficha(corte: Path, falas: list[str]) -> None:
    """Atualiza a linha da narração na ficha do corte."""
    ficha = corte.with_suffix(".txt")
    try:
        linhas = [l for l in ficha.read_text(encoding="utf-8").splitlines() if not l.startswith("Narração: ")]
    except OSError:
        return
    if falas:
        onde = next((i for i, l in enumerate(linhas) if l.startswith("Trecho original: ")), len(linhas))
        linhas.insert(onde, "Narração: " + " [corte] ".join(falas))
    ficha.write_text("\n".join(linhas) + "\n", encoding="utf-8")


def trecho(pasta: Path, arquivo: str) -> Path:
    """O trecho original guardado de um corte."""
    video, receita = pipeline.receita(pasta / arquivo)
    if not (video.is_file() and receita.is_file()):
        raise RefazerErro("este corte foi feito antes desta função existir e não guardou o vídeo original; "
                          "só os cortes novos podem ser refeitos")
    return video


def refazer(pasta: Path, arquivo: str, layout: str | None = None, camera: str | None = None,
            narrado: bool | None = None, voz: str | None = None, textos: dict | None = None) -> dict:
    """Remonta o corte e devolve o item atualizado do manifesto. `None` mantém o que o corte já tem.
    `textos` troca o que o narrador diz ({"abertura": ..., "fecho": ...}); texto novo é gravado de novo.
    Pode ser interrompido por `cancelar`: aí sobe RefazerErro e o corte fica como estava."""
    chave = (str(pasta), arquivo)
    if chave in _pedidos:
        raise RefazerErro("este corte já está sendo refeito")
    parar = _pedidos[chave] = threading.Event()
    ffmpeg.cancelavel.evento = parar
    try:
        return _refazer(pasta, arquivo, layout, camera, narrado, voz, textos, parar)
    except ffmpeg.Cancelado:
        raise RefazerErro("cancelado; o corte ficou como estava") from None
    finally:
        ffmpeg.cancelavel.evento = None
        _pedidos.pop(chave, None)


def _refazer(pasta: Path, arquivo: str, layout: str | None, camera: str | None, narrado: bool | None,
             voz: str | None, textos: dict | None, parar: threading.Event) -> dict:
    corte = pasta / arquivo
    video = trecho(pasta, arquivo)
    _, arq_receita = pipeline.receita(corte)
    dados = json.loads(arq_receita.read_text(encoding="utf-8"))

    layout = layout or dados["layout"]
    camera = dados.get("camera", "") if camera is None else camera.strip()
    if layout not in editar.LAYOUTS:
        raise RefazerErro(f"enquadramento desconhecido: {layout}")
    try:
        cam = editar.ler_camera(camera)
    except ValueError as erro:
        raise RefazerErro(str(erro)) from None
    if layout == "camera" and cam is None:
        raise RefazerErro("para a câmera em cima, marque onde fica a câmera no vídeo")

    roteiro = dict(dados.get("roteiro") or {})
    for nome, texto in (textos or {}).items():
        if nome in ("abertura", "fecho") and isinstance(texto, str):
            roteiro[nome] = " ".join(texto.split())[:400]
    narrado = dados.get("narrado", bool(dados.get("falas"))) if narrado is None else narrado
    if narrado and not any(roteiro.values()):
        raise RefazerErro("este corte não tem comentário escrito pela IA para o narrador falar")
    voz = voz if voz is not None else dados.get("voz", "")
    if voz and voz not in narrar.vozes():
        raise RefazerErro(f"voz desconhecida: {voz}")

    with _uma_de_cada_vez:
        if parar.is_set():  # cancelado enquanto esperava outro corte terminar
            raise ffmpeg.Cancelado("cancelado")
        gravadas: dict = dados.setdefault("falas", {})
        falas: dict[str, editar.Fala] = {}
        if narrado:
            info = ffmpeg.info(video)
            if not info.tem_audio:
                raise RefazerErro("o corte não tem som, então não leva narração")
            novas = []
            for nome in ("abertura", "fecho"):
                texto = roteiro.get(nome, "")
                g = gravadas.get(nome)
                if not texto:
                    continue
                if not (g and g["texto"] == texto and g.get("voz", "") == voz and (video.parent / g["arquivo"]).is_file()):
                    try:
                        n = _gravar_fala(texto, dados.get("idioma") or "pt", video.parent / f"{corte.stem}.{nome}", voz)
                    except narrar.NarracaoErro as erro:
                        raise RefazerErro(f"não deu para gravar a narração: {erro}") from None
                    if g and g["arquivo"] != n.arquivo.name:
                        (video.parent / g["arquivo"]).unlink(missing_ok=True)
                    g = gravadas[nome] = {"arquivo": n.arquivo.name, "duracao": n.duracao, "texto": texto,
                                          "traducao": "", "marcas": n.palavras, "voz": voz}
                    novas.append(g)
            if novas and dados.get("traduzir") and ia.backend():
                try:
                    for g, t in zip(novas, ia.traduzir([g["texto"] for g in novas], dados.get("assunto", ""),
                                                       dados.get("modelo"))):
                        g["traducao"] = t
                except ia.IAErro:
                    pass  # a narração sai com a legenda em um idioma só
            if novas:  # a voz paga fica registrada já: mesmo cancelando a montagem, não é gravada de novo
                arq_receita.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
            falas = {nome: editar.Fala(video.parent / g["arquivo"], g["duracao"], g["texto"], g.get("traducao", ""),
                                       g.get("marcas"))
                     for nome, g in gravadas.items() if roteiro.get(nome) == g["texto"]}

        info = ffmpeg.info(video)
        novo = video.parent / "_refazendo.mp4"
        try:
            duracao = editar.renderizar(video, info, 0.0, info.duracao, dados["palavras"], dados["titulo"], novo, layout,
                                        dados["legenda"], dados["marca"], frases=dados["frases"],
                                        gancho=dados.get("gancho", ""), apertar=dados.get("apertar", True),
                                        camera=camera, **falas)
        except Exception:
            novo.unlink(missing_ok=True)
            raise
        if parar.is_set():  # cancelado no último instante: não troca o vídeo
            novo.unlink(missing_ok=True)
            raise ffmpeg.Cancelado("cancelado")
        _trocar(novo, corte)
        editar.capa(corte, corte.with_suffix(".jpg"))
        _ficha(corte, [f.texto for f in falas.values()])

        dados.update(layout=layout, camera=camera, narrado=bool(falas), roteiro=roteiro, voz=voz)
        arq_receita.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")

    mudou = {"layout": layout, "camera": camera, "narrado": bool(falas), "duracao": round(duracao, 1),
             "abertura": falas["abertura"].texto if "abertura" in falas else "",
             "fecho": falas["fecho"].texto if "fecho" in falas else "", "editado": time.time()}
    with pipeline.TRAVA_MANIFESTO:
        manifesto = pipeline.ler_manifesto(pasta) or {}
        item = next((c for c in manifesto.get("cortes", []) if c.get("arquivo") == arquivo), None)
        if item is not None:
            item.update(mudou)
            item["roteiro"] = roteiro
            pipeline.gravar_manifesto(pasta, manifesto)
    return item or mudou
