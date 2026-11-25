"""Canal único de progresso do pipeline: o terminal imprime, a interface web guarda no trabalho."""

from __future__ import annotations

import threading


class Relator:
    """Padrão: imprime no terminal."""

    def etapa(self, n: int, total: int, texto: str) -> None:
        print(f"[{n}/{total}] {texto}", flush=True)

    def info(self, texto: str) -> None:
        print(f"    {texto}", flush=True)

    def aviso(self, texto: str) -> None:
        print(f"    {texto}", flush=True)

    def pct(self, valor: float) -> None:  # 0 a 100 dentro da etapa atual
        pass

    def titulo(self, texto: str) -> None:
        pass

    def dado(self, chave: str, valor) -> None:  # estado extra do modo ao vivo (tempo gravado, cortes feitos...)
        pass


_padrao = Relator()
_local = threading.local()  # cada trabalho roda na sua thread e fala com o seu relator: várias lives em paralelo


def usar(relator: Relator) -> None:
    """Define o relator da thread atual."""
    _local.relator = relator


def _atual() -> Relator:
    return getattr(_local, "relator", _padrao)


def etapa(n: int, total: int, texto: str) -> None:
    _atual().etapa(n, total, texto)


def info(texto: str) -> None:
    _atual().info(texto)


def aviso(texto: str) -> None:
    _atual().aviso(texto)


def pct(valor: float) -> None:
    _atual().pct(max(0.0, min(100.0, valor)))


def titulo(texto: str) -> None:
    _atual().titulo(texto)


def dado(chave: str, valor) -> None:
    _atual().dado(chave, valor)
