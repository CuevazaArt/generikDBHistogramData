# masha_live_reference

Entrada documental, no ejecutable. Preserva la lógica del bot live
original (`aportes/masha.py`) como contexto del adaptador
`library/bots/masha/`.

## Origen

- Archivo: `aportes/masha.py`
- Tipo: runner asíncrono con presets propios, gestión de TP/SL en
  tiempo real, control de cooldowns y métricas adicionales.

## Por qué se preserva

- El adaptador `masha` modela sólo el bloque "cruce + pullback + TP/SL",
  dejando fuera detalles del scheduler, cooldowns y notificaciones.
- Sirve como fuente canónica para hipótesis nuevas sobre indicadores
  o parámetros de entrada extra.

## Cómo usar

- Comparar contra el adaptador antes de proponer cambios mayores.
- No se auto-registra (`reference_only: true`).
