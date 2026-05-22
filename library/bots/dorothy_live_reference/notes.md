# dorothy_live_reference

Entrada documental, no ejecutable. Su único propósito es preservar la
lógica del bot live original (`aportes/dorothy.py`) como material de
referencia al lado del adaptador `library/bots/dorothy/`.

## Origen

- Archivo: `aportes/dorothy.py`
- Tipo: runner asíncrono basado en `binance.client.Client`, ciclo de
  trabajo con `loop_interval_sec`, presets internos A/B/C/D/E/F.

## Por qué se preserva

- El adaptador `dorothy` simplifica la lógica para encajar en
  `StrategyBase`, lo que descarta detalles operativos del live
  (gestión de claves, retries, métricas en tiempo real, drawdown
  cap dinámico).
- Cuando se itere sobre el adaptador, esta referencia evita perder de
  vista los matices que el bot productivo aplica.

## Cómo usar

- Leer `aportes/dorothy.py` cuando se quiera comparar contra el
  adaptador.
- NO añadir este entry al `STRATEGY_REGISTRY`: el flag
  `reference_only: true` previene que se registre automáticamente.
