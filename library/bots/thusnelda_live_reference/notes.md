# thusnelda_live_reference

Entrada documental, no ejecutable. Preserva el código del bot live
original (`aportes/thusnelda.py`) mientras se prepara el adaptador
backtest real.

## Origen

- Archivo: `aportes/thusnelda.py`
- Tipo: runner asíncrono con presets propios y modos
  conservador/agresivo.

## Por qué se preserva

- El adaptador actual `library/bots/thusnelda` es un placeholder hasta
  portar los modos a `StrategyBase`. Tener esta referencia evita perder
  el comportamiento esperado y los presets históricos.

## Cómo usar

- Leer el archivo original al planear el adaptador real.
- No se auto-registra (`reference_only: true`).
