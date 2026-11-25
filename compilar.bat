@echo off
rem Atalho: compilar [numero do tema] [opcoes]  -  junta cortes prontos numa compilacao horizontal
uv run --project "%~dp0." cortes-compilar %*
