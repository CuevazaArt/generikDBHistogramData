# Estado Agartha — sesion en curso

Actualizado: 2026-05-24 16:30 UTC

## Hecho hoy

- `AgarthaStrategy` implementada en `backtest/strategies.py`:
  - Compra inicial fija (`quote_order_qty_usdt`)
  - Trailing stop dinamico (`trailing_stop_pct`) sobre peak
  - Activacion condicional (`activation_profit_pct`)
  - Breakeven lock (`breakeven_lock_pct`)
  - Time stop opcional (`max_holding_bars`)
  - Partial TP opcional (`partial_tp_pct` / `partial_tp_size_pct`)
  - Single-shot por default (`allow_reentry=False`)
  - Estado serializable via `export_state` / `import_state`
- Registry: `agartha` registrado, `params_from_cli` y `suggest_params` con
  bloques propios.
- CLI: flags Agartha en `p_run` y `p_opt` (`--trailing_stop_pct`,
  `--activation_profit_pct`, etc.).
- Manifest + 3 presets: `default`, `moonshot_protected`, `partial_then_moon`.
- Notas: tesis, logica, parametros, despliegue cartera, accesorios futuros.
- Tests: `tests/test_agartha.py` (13 casos, todos verdes).
- Suite global: `test_registry` + `test_library` pasan sin regresion.

## Pendiente (proximo)

1. Conector live REST/WS Alpha (autenticacion, LIMIT-only).
2. Backtests reales sobre datos Alpha descargados (`cli.py --mode alpha_api`).
3. Orchestrator multi-instancia para cartera N x Agartha.
4. Screening del universo Alpha (token list + filtros liquidez/holders).
5. Accesorios: ATR trailing, volume gate, multi-rung partial.
6. Runs registry con metricas tipicas (hit rate, payout, Sharpe).

## Comandos de referencia

Run unico:

```powershell
$env:PYTHONPATH='.'
python backtest_cli.py --db klines.db run `
  --strategy agartha --symbol ALPHA_175USDT --interval 1m `
  --initial_cash 100 --quote_order_qty_usdt 10 `
  --trailing_stop_pct 30 --activation_profit_pct 50 --breakeven_lock_pct 100
```

Tests:

```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_agartha.py -q
```
