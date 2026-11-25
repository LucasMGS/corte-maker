"""Configuração do corte-maker: cada valor vem do ambiente ou, na falta, do arquivo `.env` na raiz do projeto.

O `.env` não entra no git. O modelo com todas as opções é o `.env.example`.
"""

from __future__ import annotations

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARQUIVO = RAIZ / ".env"


def config(nome: str, padrao: str = "") -> str:
    """Valor de uma configuração: do ambiente ou, na falta, do `.env`. Vazio conta como ausente."""
    if os.environ.get(nome):
        return os.environ[nome]
    try:
        for linha in ARQUIVO.read_text(encoding="utf-8-sig").splitlines():
            linha = linha.strip()
            if not linha or linha.startswith("#"):
                continue
            chave, _, valor = linha.partition("=")
            if chave.strip().removeprefix("export ").strip() == nome and valor.strip():
                return valor.strip().strip('"').strip("'")
    except OSError:
        pass
    return padrao
