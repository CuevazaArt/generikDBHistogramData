# Estado Agartha — handoff entre sesiones

Actualizado: 2026-05-25 07:42 UTC (cierre de sesion intensiva)

## Resumen de la sesion 2026-05-24

Desarrollo completo del bot Agartha desde scaffold hasta universo Alpha
cubierto. 18 tags, ~12 horas de trabajo. Estado: **listo para connector live**.

### Hitos
- Strategy `AgarthaStrategy` + accesorios (trailing/breakeven/partial/LIMIT entry)
- Pipeline canonico Alpha (`scripts/agartha_alpha_study.py`)
- Optuna spectrum bimodal con extremos
- Paralelizacion 10 workers (3x speedup)
- Walk-forward validation (40 % generaliza OOS)
- **n=386 symbols evaluados** (77 % positivos, top M +6810 %)
- 8 reglas arbitrarias documentadas + tests de regresion
- 1 bug critico cazado y corregido (prefix collision)

### Para arrancar manana

1. **Revisar este STATE.md y `library/bots/agartha/ALPHA_STUDY_MODEL.md`**.
2. **Siguientes pasos candidatos** (no priorizados):
   - Connector live REST/WS Alpha (toolkit listo, pendiente la pieza de red)
   - Dashboard de cartera (visualizar las 28 mega-winners con sus seteos)
   - Re-validar walk-forward con n=386 (mas robusto que n=30)
   - Re-optimizacion rolling (semanal, automatica)
   - Sistema de monitoreo + alertas en vivo (telegram/email)
3. `git log` muestra 18 tags secuenciales del progreso.

### Originalmente (sesion previa)

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
