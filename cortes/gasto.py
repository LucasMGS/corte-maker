"""Conta o que cada trabalho gasta: chamadas e tokens da IA, caracteres de voz paga e tempo de transcrição.

Cada trabalho (um vídeo ou uma live) abre a sua conta na thread em que roda; quem faz uma chamada paga só avisa
`gasto.ia(...)`, `gasto.voz(...)` ou `gasto.transcricao(...)`, sem precisar receber a conta por parâmetro.
No fim, o resumo vai para o manifesto do trabalho e uma linha entra em `gastos.csv`, na raiz do projeto.
"""

from __future__ import annotations

import csv
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PLANILHA = Path(__file__).resolve().parent.parent / "gastos.csv"

# US$ por milhão de tokens (entrada, saída), para estimar o custo quando a resposta não traz o valor (API direta).
# O Claude Code já devolve o custo de cada chamada, e nesse caso é ele que vale. Confira os preços de tempos em tempos.
PRECOS = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


def _estimar(modelo: str, entrada: int, saida: int, cache_lido: int, cache_escrito: int) -> float | None:
    for prefixo, (p_entrada, p_saida) in PRECOS.items():
        if modelo.startswith(prefixo):
            return (entrada * p_entrada + saida * p_saida + cache_lido * p_entrada * 0.1
                    + cache_escrito * p_entrada * 1.25) / 1e6
    return None


@dataclass
class Conta:
    chamadas: dict[str, int] = field(default_factory=dict)  # por tipo: "analise", "traducao"
    entrada: int = 0  # tokens de entrada, fora os de cache
    saida: int = 0
    cache_lido: int = 0
    cache_escrito: int = 0
    custo_usd: float = 0.0
    sem_preco: int = 0  # chamadas cujo custo não deu para saber nem estimar
    modelos: set[str] = field(default_factory=set)
    voz_caracteres: int = 0
    voz_chamadas: int = 0
    transcrito_s: float = 0.0
    _trava: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def somar_ia(self, tipo: str, modelos: list[str], entrada: int, saida: int, cache_lido: int = 0,
                 cache_escrito: int = 0, custo_usd: float | None = None) -> None:
        if custo_usd is None and modelos:
            custo_usd = _estimar(modelos[0], entrada, saida, cache_lido, cache_escrito)
        with self._trava:
            self.chamadas[tipo] = self.chamadas.get(tipo, 0) + 1
            self.entrada += entrada
            self.saida += saida
            self.cache_lido += cache_lido
            self.cache_escrito += cache_escrito
            self.modelos.update(modelos)
            if custo_usd is None:
                self.sem_preco += 1
            else:
                self.custo_usd += custo_usd

    def somar_voz(self, caracteres: int) -> None:
        with self._trava:
            self.voz_caracteres += caracteres
            self.voz_chamadas += 1

    def somar_transcricao(self, segundos: float) -> None:
        with self._trava:
            self.transcrito_s += segundos

    def resumo(self) -> dict:
        with self._trava:
            return {
                "chamadas_ia": sum(self.chamadas.values()), "chamadas_por_tipo": dict(self.chamadas),
                "tokens_entrada": self.entrada, "tokens_saida": self.saida,
                "tokens_cache_lido": self.cache_lido, "tokens_cache_escrito": self.cache_escrito,
                "custo_ia_usd": round(self.custo_usd, 4), "chamadas_sem_preco": self.sem_preco,
                "modelos": sorted(self.modelos),
                "voz_caracteres": self.voz_caracteres, "voz_chamadas": self.voz_chamadas,
                "transcrito_min": round(self.transcrito_s / 60, 1),
            }

    def frase(self) -> str:
        r = self.resumo()
        partes = [f"IA: {r['chamadas_ia']} chamada(s), {r['tokens_entrada'] + r['tokens_cache_lido'] + r['tokens_cache_escrito']} "
                  f"tokens de entrada e {r['tokens_saida']} de saída, cerca de US$ {r['custo_ia_usd']:.2f}"]
        if r["voz_caracteres"]:
            partes.append(f"voz paga: {r['voz_caracteres']} caracteres")
        if r["transcrito_min"]:
            partes.append(f"transcrição: {r['transcrito_min']:.0f} min de áudio")
        return "Gasto deste trabalho | " + " | ".join(partes)


_local = threading.local()


def abrir() -> Conta:
    """Começa a conta do trabalho que roda nesta thread."""
    _local.conta = Conta()
    return _local.conta


def atual() -> Conta | None:
    return getattr(_local, "conta", None)


def usar(conta: Conta | None) -> None:
    """Faz esta thread somar na conta de outra (as análises em paralelo rodam em threads auxiliares)."""
    _local.conta = conta


def ia(tipo: str, modelos: list[str], entrada: int, saida: int, cache_lido: int = 0, cache_escrito: int = 0,
       custo_usd: float | None = None) -> None:
    if (conta := atual()) is not None:
        conta.somar_ia(tipo, modelos, entrada, saida, cache_lido, cache_escrito, custo_usd)


def voz(caracteres: int) -> None:
    if (conta := atual()) is not None:
        conta.somar_voz(caracteres)


def transcricao(segundos: float) -> None:
    if (conta := atual()) is not None:
        conta.somar_transcricao(segundos)


_trava_planilha = threading.Lock()
COLUNAS = ["quando", "tipo", "titulo", "url", "horas_analisadas", "cortes", "chamadas_ia", "tokens_entrada",
           "tokens_saida", "custo_ia_usd", "custo_ia_usd_por_hora", "voz_caracteres", "transcrito_min", "modelos"]


def registrar(conta: Conta, tipo: str, titulo: str, url: str, segundos_analisados: float, cortes: int) -> None:
    """Acrescenta uma linha a `gastos.csv` (ponto e vírgula e vírgula decimal, para abrir direto no Excel em português)."""
    r = conta.resumo()
    horas = segundos_analisados / 3600
    numero = lambda v, casas: f"{v:.{casas}f}".replace(".", ",")
    linha = [time.strftime("%Y-%m-%d %H:%M"), tipo, titulo, url, numero(horas, 2), cortes, r["chamadas_ia"],
             r["tokens_entrada"] + r["tokens_cache_lido"] + r["tokens_cache_escrito"], r["tokens_saida"],
             numero(r["custo_ia_usd"], 4), numero(r["custo_ia_usd"] / horas, 4) if horas > 0.01 else "",
             r["voz_caracteres"], numero(r["transcrito_min"], 1), " ".join(r["modelos"])]
    try:
        with _trava_planilha:
            nova = not PLANILHA.exists()
            with PLANILHA.open("a", newline="", encoding="utf-8-sig") as arquivo:
                escritor = csv.writer(arquivo, delimiter=";")
                if nova:
                    escritor.writerow(COLUNAS)
                escritor.writerow(linha)
    except OSError:
        pass  # planilha aberta no Excel ou disco cheio: o trabalho não falha por causa do registro
