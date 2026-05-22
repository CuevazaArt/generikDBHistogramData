# Multi-symbol (Fase 4)

## Que es y para que sirve

`multi-symbol` corre la **misma estrategia con los mismos parametros** sobre
una canasta de simbolos. Es la prueba de **robustez cruzada**: si una
configuracion solo funciona en un activo y se desploma en los demas, lo mas
probable es que estemos viendo un comportamiento idiosincratico (o suerte) y
no una ventaja generalizable.

Casos de uso tipicos:

- Validar que un seteo bueno en `XRPUSDT` no se cae al cambiar a `BTCUSDT` o
  `ETHUSDT`.
- Construir "panels" de pruebas estandar que se corren cada vez que se
  publica una nueva variante de bot.
- Identificar cual es el peor simbolo (`worst_symbol`) y la dispersion entre
  el mejor y el peor para ajustar expectativas operativas.

## Comando rapido

```bash
python backtest_cli.py \
  --db klines.db \
  multi-symbol \
  --strategy dorothy \
  --symbols BTCUSDT,XRPUSDT,ETHUSDT \
  --interval 1h \
  --output_dir reports/multi_symbol/dorothy_basket
```

Flags adicionales:

- `--start_ts` / `--end_ts` (ms UTC): si se omiten, cada simbolo usa el
  rango disponible en su tabla de klines. Util para pruebas rapidas.
- `--initial_cash_per_symbol` (default `10000.0`): cada simbolo recibe su
  propio bankroll; los simbolos NO comparten capital.
- `--fee_rate`, `--slippage_bps`: identicos a los de `run`.

## Limitacion actual: `--share_cash_pool`

El flag `--share_cash_pool` esta aceptado en la CLI **pero no esta
implementado todavia**: invocarlo dispara un `NotImplementedError(
"joint-pool multi-symbol is reserved for a future phase")`. Esta reservado
para una fase futura del rediseño en la que el orquestador maneja un solo
banco de capital compartido entre simbolos. La forma del flag se mantiene
estable para que los scripts ya existentes no rompan cuando llegue el
soporte.

## Archivos generados

Bajo `--output_dir`:

| Archivo | Contenido |
|---|---|
| `multi_symbol_report.md` | Resumen humano: configuracion, mejor/peor simbolo, tabla por simbolo. |
| `per_symbol_summary.csv` | Detalle por simbolo: `symbol`, `run_id`, `total_return`, `sharpe`, `win_rate`, `num_trades`, `final_equity`. |

Y por cada simbolo, los artefactos del run individual:

| Ruta | Contenido |
|---|---|
| `data/events/run_<run_id>/...` | Eventos del run. |
| `data/equity/run_<run_id>/equity.parquet` | Curva de capital. |

## Como leer el reporte

- `mean_total_return` y `median_total_return` resumen el desempeño tipico.
  Si son muy diferentes, hay un simbolo dominando la media (outlier).
- `dispersion_pct = best - worst`: cuanto mas alto, menos transferible es el
  seteo entre activos. Una dispersion alta con `mean` positivo todavia puede
  ser util si se acepta cherry-picking explicito; una baja con mean positivo
  es lo que buscamos para considerar la estrategia robusta.
- `joint_capital_curve` queda en `null`: solo lo poblara la fase futura del
  joint pool.
