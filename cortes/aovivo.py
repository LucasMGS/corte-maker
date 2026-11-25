"""Modo ao vivo: grava a live em pedaços e, a cada poucos minutos, analisa o trecho novo e publica os cortes
que passarem da nota mínima, enquanto a transmissão continua."""

from __future__ import annotations

import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import audio, baixar, ffmpeg, gasto, ia, momentos, pipeline, progresso, transcrever
from .momentos import Momento, hms
from .pipeline import Opcoes

PEDACO = 20  # segundos de cada arquivo gravado
MARGEM = 6  # um momento que termina a menos disso do fim da janela pode estar incompleto: fica para a próxima
POR_JANELA = 2  # no máximo quantos cortes novos por análise
FALHAS = pipeline.RAIZ / "trabalho" / "falhas.log"  # traceback completo de cada análise ou corte manual que falhou
INSISTENCIA = 3  # falhas seguidas de análise até o aviso de que a live está só sendo gravada


def _registrar_falha(o_que: str, erro: BaseException) -> str:
    """Guarda o traceback completo em `trabalho/falhas.log` e devolve onde o erro nasceu dentro do programa
    (ex.: "aovivo._analisar:176 > ffmpeg.rodar:20"), para o aviso da interface dizer mais que a mensagem do erro."""
    quadros = traceback.extract_tb(erro.__traceback__)
    nossos = [q for q in quadros if Path(q.filename).parent == Path(__file__).parent] or quadros
    origem = " > ".join(f"{Path(q.filename).stem}.{q.name}:{q.lineno}" for q in nossos[-2:])
    try:
        with FALHAS.open("a", encoding="utf-8") as arquivo:
            arquivo.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} | {o_que}\n"
                          f"{''.join(traceback.format_exception(erro))}\n")
    except OSError:
        pass  # o registro é um extra: não pode virar outra falha
    return origem or type(erro).__name__


@dataclass
class Pedaco:
    caminho: Path
    inicio: float  # segundos desde o começo da gravação
    duracao: float

    @property
    def fim(self) -> float:
        return self.inicio + self.duracao


class Gravador:
    """Um ffmpeg copiando a live para arquivos .ts de ~20 s, sem recodificar."""

    def __init__(self, url: str, pasta: Path) -> None:
        self.url, self.pasta = url, pasta
        self.proc: subprocess.Popen | None = None
        self.pedacos: list[Pedaco] = []
        self._vistos: set[Path] = set()
        self._relogio = 0.0
        self.quebrados = 0  # pedaços inválidos seguidos: sinal de que a gravação saiu do eixo e precisa recomeçar

    def consultar(self) -> dict:
        """Dados da live (título, canal, se está no ar) e os endereços dos fluxos."""
        import yt_dlp

        opcoes = {"quiet": True, "no_warnings": True, "noplaylist": True,
                  "format": "bv*+ba/b", "format_sort": ["res:1080", "vcodec:h264", "acodec:aac"],
                  "js_runtimes": {"deno": {}, "node": {}}}
        with yt_dlp.YoutubeDL(opcoes) as ydl:
            return ydl.extract_info(self.url, download=False)

    def iniciar(self, dados: dict) -> None:
        fluxos = dados.get("requested_formats") or [dados]
        cmd = [ffmpeg.exe(), "-hide_banner", "-loglevel", "error", "-y"]
        for f in fluxos:
            cabecalhos = "".join(f"{k}: {v}\r\n" for k, v in (f.get("http_headers") or {}).items())
            cmd += ["-headers", cabecalhos, "-i", f["url"]]
        # YouTube entrega vídeo e áudio em fluxos separados; Twitch e Kick, juntos
        cmd += ["-map", "0:v:0", "-map", "1:a:0" if len(fluxos) > 1 else "0:a:0", "-c", "copy"]
        if len(fluxos) > 1:
            cmd += ["-copyts"]  # mantém os dois fluxos no mesmo relógio (sincronia de áudio)
        # Com um fluxo só (Twitch, Kick) não se usa copyts: quando entra um anúncio o relógio da transmissão dá um
        # salto, e é sem copyts que o ffmpeg corrige o salto. Com copyts os pedaços saíam de 2 s ou sem duração.
        cmd += ["-f", "segment", "-segment_time", str(PEDACO), "-segment_format", "mpegts", "-reset_timestamps", "1",
                "-segment_start_number", str(len(self._vistos)), str(self.pasta / "p_%06d.ts")]
        # grupo próprio: um Ctrl+C no terminal não derruba a gravação antes de fecharmos o último pedaço
        grupo = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        with open(self.pasta / "gravador.log", "ab") as registro:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=registro,
                                         creationflags=grupo)

    def ultimo_erro(self) -> str:
        try:
            return (self.pasta / "gravador.log").read_text(encoding="utf-8", errors="replace").strip()[-400:]
        except OSError:
            return ""

    def gravando(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def parar(self) -> None:
        if self.gravando():
            try:
                self.proc.communicate(b"q", timeout=10)  # "q" faz o ffmpeg fechar o arquivo direito
            except (subprocess.TimeoutExpired, OSError):
                self.proc.kill()
                self.proc.wait()

    def prontos(self) -> list[Pedaco]:
        """Pedaços já fechados e ainda em disco. Enquanto grava, o arquivo mais novo está incompleto e fica de fora."""
        arquivos = sorted(self.pasta.glob("p_*.ts"))
        if self.gravando():
            arquivos = arquivos[:-1]
        for arq in arquivos:
            if arq in self._vistos:
                continue
            self._vistos.add(arq)
            try:
                duracao = ffmpeg.info(arq).duracao
            except Exception:  # pedaço vazio ou truncado numa queda de conexão
                duracao = 0.0
            if duracao > 0.5:
                self.pedacos.append(Pedaco(arq, self._relogio, duracao))
                self._relogio += duracao
                self.quebrados = 0
            else:
                self.quebrados += 1
                arq.unlink(missing_ok=True)  # não serve para nada e, acumulando, encheria o disco
        return self.pedacos

    def recomecar(self, dados: dict) -> None:
        """Fecha a gravação atual e abre outra do zero, continuando a numeração dos pedaços."""
        self.parar()
        self.prontos()  # conta o último arquivo, para o novo ffmpeg não escrever por cima dele
        self.quebrados = 0
        self.iniciar(dados)

    def descartar_ate(self, t: float) -> None:
        """Apaga do disco o que já foi analisado: uma live de horas não pode encher o HD."""
        for p in [p for p in self.pedacos if p.fim <= t]:
            p.caminho.unlink(missing_ok=True)
            self.pedacos.remove(p)


def _achar(o: Opcoes, linhas: list, energia, titulo: str, canal: str, duracao: float, situacao: dict) -> list[Momento]:
    situacao["triagem"] = None  # nota da triagem, quando foi ela que descartou o trecho
    if not o.sem_ia and len(linhas) >= 5 and ia.backend():
        contexto = (o.contexto + " " if o.contexto else "") + "Este é um trecho de uma live que ainda está acontecendo."
        try:
            if o.triagem and not ia.modelo_barato(o.modelo):
                try:
                    nota, _ = momentos.triar(linhas, titulo, canal, contexto, o.dmin, o.dmax)
                except ia.IAErro:
                    nota = 100  # triagem indisponível: a análise completa decide (e avisa, se também falhar)
                if nota < o.limiar - momentos.FOLGA_TRIAGEM:
                    situacao["triagem"] = nota
                    return []
            achados = momentos.por_ia(linhas, titulo, canal, contexto, POR_JANELA, o.dmin, o.dmax, o.modelo, o.narracao,
                                      o.abertura)
            if situacao["ia_falhou"]:
                progresso.info("A IA voltou a responder.")
                situacao["ia_falhou"] = False
            return achados
        except ia.IAErro as erro:
            if not situacao["ia_falhou"]:  # avisa uma vez; segue tentando a cada análise
                dica = " Se for login expirado: abra um terminal, rode `claude` e digite /login." if ia.backend() == "cli" else ""
                progresso.aviso(f"IA indisponível ({erro}).{dica} Enquanto isso, só picos de áudio viram corte.")
                situacao["ia_falhou"] = True
    elif not situacao["avisou_sem_ia"] and not o.sem_ia and not ia.backend():
        progresso.aviso("Claude não encontrado: os cortes são escolhidos só por picos de áudio.")
        situacao["avisou_sem_ia"] = True
    return momentos.por_audio(energia, linhas, titulo, POR_JANELA, o.dmin, o.dmax, duracao, absoluta=True)


def _analisar(o: Opcoes, janela: list[Pedaco], trabalho: Path, pasta: Path, manifesto: dict, emitidos: list[tuple],
              situacao: dict, analisado: float, final: bool) -> None:
    """Junta os pedaços da janela, procura momentos e publica os que passam da nota mínima."""
    t0 = janela[0].inicio
    video = trabalho / "janela.mkv"
    lista = trabalho / "janela.txt"
    lista.write_text("".join(f"file '{p.caminho.name}'\n" for p in janela), encoding="utf-8")
    ffmpeg.rodar(["-f", "concat", "-safe", "0", "-i", lista.name, "-c", "copy", video.name], cwd=trabalho)

    info = ffmpeg.info(video)
    wav = trabalho / "janela.wav"
    wav.unlink(missing_ok=True)
    audio.extrair_wav(video, wav)
    # o volume "normal" vem dos últimos 30 min de live, não só desta janela: a régua fica igual entre análises
    db = audio.volume(wav)
    inedito = max(1, int((t0 + info.duracao - analisado) / audio.PASSO))
    situacao["volume"] = np.concatenate([situacao["volume"], db[-inedito:]])[-int(1800 / audio.PASSO):]
    energia = audio.normalizar(db, situacao["volume"])
    idioma = None if o.idioma == "auto" else o.idioma
    palavras = transcrever.transcrever(wav, None, o.whisper, idioma, 0)["palavras"]
    linhas = momentos.montar_linhas(palavras, energia)
    achados = _achar(o, linhas, energia, manifesto["titulo"], manifesto["canal"], info.duracao, situacao)

    fonte = baixar.Fonte(manifesto["titulo"], manifesto["canal"], manifesto["url"], video, trabalho)
    melhor = max((m.nota for m in achados), default=0)
    novos = 0
    for m in sorted(achados, key=lambda m: m.inicio):
        ini, fim = t0 + m.inicio, t0 + m.fim
        if m.nota < o.limiar or (not final and m.fim > info.duracao - MARGEM):
            continue
        if any(min(fim, b) - max(ini, a) > 1.0 for a, b in emitidos):
            continue  # já virou corte numa análise anterior (as janelas se sobrepõem)
        m.fim = min(m.fim, info.duracao)
        if m.origem == "audio":
            m.titulo = f"{manifesto['titulo'][:48]} — {hms(ini)}"
        item = pipeline.publicar(m, len(manifesto["cortes"]) + 1, pasta, fonte, info, palavras, o, t0)
        manifesto["cortes"].append(item)
        emitidos.append((ini, fim))
        novos += 1
        pipeline.gravar_manifesto(pasta, manifesto)
        progresso.dado("n_cortes", len(manifesto["cortes"]))
        progresso.dado("ultimo", m.titulo)
        progresso.info(f"Novo corte: {m.titulo} | nota {m.nota} | {hms(ini)} | {m.fim - m.inicio:.0f}s")
    if not novos:
        detalhe = f" (melhor nota: {melhor}, mínimo: {o.limiar})" if achados and melhor < o.limiar else ""
        if situacao.get("triagem") is not None:
            detalhe = (f" (triagem rápida: nota {situacao['triagem']}; a análise completa roda a partir de "
                       f"{o.limiar - momentos.FOLGA_TRIAGEM})")
        progresso.info(f"{hms(t0)}–{hms(t0 + info.duracao)}: nenhum corte novo{detalhe}.")


def _cortar_manual(o: Opcoes, pedacos: list[Pedaco], inicio: float, fim: float, trabalho: Path, pasta: Path,
                   manifesto: dict, emitidos: list[tuple]) -> None:
    """Corte pedido à mão: junta os pedaços que cobrem o trecho, transcreve e publica. A IA só escreve os textos."""
    usados = [p for p in pedacos if p.fim > inicio and p.inicio < fim]
    if not usados:
        progresso.aviso(f"Corte manual em {hms(fim)}: esse trecho já saiu do disco.")
        return
    t0 = usados[0].inicio
    inicio, fim = max(inicio, t0), min(fim, usados[-1].fim)
    if fim - inicio < 3:
        progresso.aviso(f"Corte manual em {hms(fim)}: sobrou menos de 3 s gravados desse trecho.")
        return
    video, lista, wav = trabalho / "manual.mkv", trabalho / "manual.txt", trabalho / "manual.wav"
    try:
        lista.write_text("".join(f"file '{p.caminho.name}'\n" for p in usados), encoding="utf-8")
        ffmpeg.rodar(["-f", "concat", "-safe", "0", "-i", lista.name, "-c", "copy", video.name], cwd=trabalho)
        info = ffmpeg.info(video)
        wav.unlink(missing_ok=True)
        audio.extrair_wav(video, wav)
        idioma = None if o.idioma == "auto" else o.idioma
        palavras = transcrever.transcrever(wav, None, o.whisper, idioma, 0)["palavras"]
        a, b = inicio - t0, min(fim - t0, info.duracao)
        do_trecho = [p for p in palavras if p["i"] >= a - 0.05 and p["f"] <= b + 0.05]
        linhas = momentos.montar_linhas(do_trecho, np.zeros(int(info.duracao / audio.PASSO) + 2, dtype=np.float32))
        m = None
        if not o.sem_ia and ia.backend():
            try:
                m = momentos.descrever(linhas, manifesto["titulo"], manifesto["canal"], o.contexto, b - a, o.modelo,
                                       o.narracao, o.abertura)
            except ia.IAErro as erro:
                progresso.aviso(f"IA indisponível para o corte manual ({erro}). Ele sai com título genérico.")
        if m is None:
            m = Momento(0.0, 0.0, 0, f"{manifesto['titulo'][:48]} — {hms(inicio)}", origem="manual")
        m.inicio, m.fim = a, b
        fonte = baixar.Fonte(manifesto["titulo"], manifesto["canal"], manifesto["url"], video, trabalho)
        item = pipeline.publicar(m, len(manifesto["cortes"]) + 1, pasta, fonte, info, palavras, o, t0)
        manifesto["cortes"].append(item)
        emitidos.append((inicio, fim))  # a análise automática não repete este trecho
        pipeline.gravar_manifesto(pasta, manifesto)
        progresso.dado("n_cortes", len(manifesto["cortes"]))
        progresso.dado("ultimo", m.titulo)
        progresso.info(f"Corte manual: {m.titulo} | {hms(inicio)} | {b - a:.0f}s")
    finally:
        for arquivo in (video, lista, wav):
            arquivo.unlink(missing_ok=True)


def _atender_manuais(manuais: queue.Queue | None, o: Opcoes, pedacos: list[Pedaco], gravado: float, trabalho: Path,
                     pasta: Path, manifesto: dict, emitidos: list[tuple]) -> None:
    """Faz os cortes pedidos à mão desde a última volta. Cada pedido é (fim, duração): o corte termina em `fim`
    (segundos da gravação; None = o mais recente que já está gravado) e pega `duração` segundos para trás."""
    while manuais is not None:
        try:
            fim, duracao = manuais.get_nowait()
        except queue.Empty:
            return
        fim = gravado if fim is None else min(float(fim), gravado)
        progresso.dado("fase", "cortando")
        try:
            _cortar_manual(o, pedacos, fim - duracao, fim, trabalho, pasta, manifesto, emitidos)
        except Exception as erro:  # um corte manual que falha não derruba o monitoramento
            origem = _registrar_falha(f"{manifesto['titulo']} | corte manual em {hms(fim)}", erro)
            progresso.aviso(f"Falha no corte manual em {hms(fim)}: {erro} [{origem}]")
        progresso.dado("fase", "gravando")


def monitorar(url: str, o: Opcoes, parar: threading.Event, manuais: queue.Queue | None = None) -> dict | None:
    """Acompanha a live até ela acabar ou `parar` ser acionado. Devolve o manifesto, ou None se nenhum corte saiu.
    `manuais` recebe pedidos de corte feitos à mão enquanto a live roda (ver `_atender_manuais`)."""
    o.validar()
    if not baixar.eh_link(url):
        raise RuntimeError("o modo ao vivo precisa do link da live")
    conta = gasto.abrir()
    progresso.etapa(1, 1, "Conectando à live")
    # o sufixo evita choque quando duas lives começam no mesmo segundo
    trabalho = pipeline.RAIZ / "trabalho" / f"aovivo_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    trabalho.mkdir(parents=True)
    progresso.dado("trabalho", trabalho.name)  # a interface acha por aqui os pedaços gravados, para a prévia
    grav = Gravador(url, trabalho)
    try:
        dados = grav.consultar()
        if not dados.get("is_live"):
            raise RuntimeError("este link não está ao vivo agora; para um vídeo já gravado, use o modo normal")
        titulo = dados.get("title") or url
        progresso.titulo(titulo)
        pasta = pipeline.pasta_nova(o.saida, pipeline.nome_arquivo(f"AO VIVO - {titulo}", 60))
        manifesto = {
            "pasta": pasta.name, "titulo": titulo, "canal": dados.get("channel") or dados.get("uploader") or "",
            "url": dados.get("webpage_url") or url, "criado_em": time.time(), "layout": o.layout,
            "ao_vivo": True, "janela_inicio": 0.0, "janela_fim": 0.0, "cortes": [],
        }
        pipeline.gravar_manifesto(pasta, manifesto)
        progresso.dado("pasta", pasta.name)
        grav.iniciar(dados)
        progresso.info(f"Gravando. Primeira análise em cerca de {o.intervalo // 60} min {o.intervalo % 60:02d} s.")

        sobreposicao = o.dmax + 30  # o que é reanalisado na janela seguinte, para pegar momentos que cruzam a emenda
        emitidos: list[tuple] = []
        situacao = {"ia_falhou": False, "avisou_sem_ia": False, "triagem": None,
                    "volume": np.zeros(0, dtype=np.float32)}
        analisado, religadas, recomecos, falhas = 0.0, 0, 0, 0
        while True:
            if not grav.gravando() and not parar.is_set() and religadas < 5:
                # o ffmpeg saiu: ou a live acabou, ou a conexão caiu. Se ainda estiver no ar, religa.
                try:
                    dados = grav.consultar()
                except Exception:
                    dados = {}
                if dados.get("is_live"):
                    religadas += 1
                    progresso.info("A conexão com a live caiu. Reconectando...")
                    grav.iniciar(dados)
            if grav.quebrados >= 4 and grav.gravando() and not parar.is_set() and recomecos < 30:
                # vários pedaços inválidos seguidos: a transmissão mudou no meio (anúncio, troca de qualidade)
                recomecos += 1
                progresso.info("A gravação saiu do eixo (anúncio ou corte na transmissão). Recomeçando a gravação...")
                try:
                    grav.recomecar(grav.consultar())
                except Exception as erro:
                    progresso.aviso(f"Não consegui recomeçar a gravação: {erro}")
            final = parar.is_set() or not grav.gravando()
            if final:
                progresso.dado("fase", "encerrando")
                grav.parar()
            pedacos = grav.prontos()
            gravado = pedacos[-1].fim if pedacos else analisado
            manifesto["janela_fim"] = gravado
            progresso.dado("decorrido", round(gravado))
            progresso.dado("proxima", max(0, round(o.intervalo - (gravado - analisado))))
            if pedacos:
                ultimo = pedacos[-1]
                progresso.dado("previa", {"arq": ultimo.caminho.stem, "inicio": round(ultimo.inicio, 2),
                                          "duracao": round(ultimo.duracao, 2)})
            _atender_manuais(manuais, o, pedacos, gravado, trabalho, pasta, manifesto, emitidos)

            if gravado - analisado >= (5 if final else o.intervalo):
                inicio_janela = max(0.0, analisado - sobreposicao)
                janela = [p for p in pedacos if p.fim > inicio_janela]
                progresso.dado("fase", "analisando")
                try:
                    _analisar(o, janela, trabalho, pasta, manifesto, emitidos, situacao, analisado, final)
                    if falhas:
                        progresso.info(f"A análise voltou a funcionar depois de {falhas} falha(s) seguida(s).")
                    falhas = 0
                except Exception as erro:  # uma análise que falha não derruba o monitoramento
                    falhas += 1
                    trecho = f"{hms(inicio_janela)}–{hms(gravado)}"
                    origem = _registrar_falha(f"{manifesto['titulo']} | análise de {trecho}", erro)
                    texto = f"Falha ao analisar {trecho}: {erro} [{origem}]"
                    if falhas < INSISTENCIA:
                        progresso.aviso(texto)
                    elif falhas == INSISTENCIA:
                        progresso.aviso(
                            f"{texto} É a {falhas}ª falha seguida: a live continua sendo gravada, mas não está sendo "
                            "analisada. Sigo tentando a cada análise; se não voltar sozinha, feche e abra o programa. "
                            f"Os detalhes de cada falha ficam em trabalho\\{FALHAS.name}; as próximas repetições vão "
                            "só para o registro.")
                    else:  # o aviso já foi dado: repetir a cada análise só encheria a tela
                        progresso.info(texto)
                analisado = gravado
                # pedidos feitos durante a análise: atende antes de apagar os pedaços de que eles precisam
                _atender_manuais(manuais, o, pedacos, gravado, trabalho, pasta, manifesto, emitidos)
                grav.descartar_ate(analisado - sobreposicao)
                manifesto["gasto"] = conta.resumo()
                progresso.dado("gasto", manifesto["gasto"])
                pipeline.gravar_manifesto(pasta, manifesto)
                if not final:
                    progresso.dado("fase", "gravando")
            if final:
                break
            parar.wait(2)

        if analisado == 0 and not parar.is_set():
            shutil.rmtree(pasta, ignore_errors=True)
            raise RuntimeError(f"não consegui gravar a live. {grav.ultimo_erro()}".strip())
        manifesto["ao_vivo"] = False
        manifesto["gasto"] = pipeline._fechar_conta(conta, "live", titulo, manifesto["url"], analisado,
                                                    len(manifesto["cortes"]))
        if manifesto["cortes"]:
            pipeline.gravar_manifesto(pasta, manifesto)
        else:
            shutil.rmtree(pasta, ignore_errors=True)
        motivo = "parado por você" if parar.is_set() else "a live acabou"
        progresso.info(f"Monitoramento encerrado ({motivo}): {hms(analisado)} acompanhados, {len(manifesto['cortes'])} corte(s).")
        return manifesto if manifesto["cortes"] else None
    finally:
        grav.parar()
        shutil.rmtree(trabalho, ignore_errors=True)
