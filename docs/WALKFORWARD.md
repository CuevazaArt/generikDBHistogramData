# Walk-forward (Fase 4)

## Que es y por que importa

Walk-forward es un protocolo de evaluacion en el que la estrategia se prueba
una y otra vez sobre tramos consecutivos de la historia. Cada fold tiene dos
ventanas:

- **Train (in-sample):** la ventana en la que se ajustan o validan parametros.
- **Test (out-of-sample):** la ventana inmediatamente posterior, donde se mide
  como rinde la misma configuracion sobre datos que la estrategia no vio.

La pregunta de negocio que responde es la misma cada fold: *si hubiera elegido
estos parametros con la informacion disponible al inicio del periodo de test,
¿como me habria ido?*. Repetirlo decenas de veces a lo largo de la historia
hace muy dificil que un resultado afortunado se cuele como bueno: si train
brilla pero test colapsa, lo que tenemos es **sobreajuste** y no una
estrategia robusta.

## Comando rapido

```bash
python backtest_cli.py \
  --db klines.db \
  walk-forward \
  --strategy dorothy \
  --symbol XRPUSDT \
  --interval 1h \
  --start_ts 1704067200000 \
  --end_ts 1735603200000 \
  --train_window_days 90 \
  --test_window_days 30 \
  --step_days 30 \
  --output_dir reports/walkforward/dorothy_xrp
```

Opciones relevantes:

- `--anchored`: cambia el modo a *expanding window*. Train siempre arranca en
  `start_ts` y crece fold a fold; test sigue siendo la ventana posterior.
- `--optimize_per_fold`: corre una pequena busqueda Optuna sobre cada train
  (`--trials_per_fold N`, por defecto 30) y adopta `best_params` para evaluar
  el test. Ideal para reproducir el flujo "lo que un trader haria realmente".
- `--initial_cash`, `--fee_rate`, `--slippage_bps`: replican los flags
  estandar del subcomando `run`.

Convertimos `dias -> ms` con `int(days * 86_400_000)` para que la
construccion de ventanas sea exacta y reproducible (sin sorpresas por
`timedelta` en cambios de horario).

## Como interpretar `decay_test_vs_train_pct`

El reporte calcula:

```
decay_test_vs_train_pct = (train_mean_total_return - test_mean_total_return)
                          / train_mean_total_return * 100
```

- **`> 25%`**: decaimiento alto. El parametro funciona "demasiado bien" en
  train; cuidado con sobreajuste.
- **`5% - 25%`**: decaimiento moderado. Revisa si los parametros son estables
  entre folds o si hay derivas grandes.
- **`-5% - 5%`**: train y test bastante alineados; la estrategia generaliza.
- **`< -5%`**: el test rinde mejor que el train. Suele indicar mucho ruido en
  los folds o ventanas demasiado cortas; vale la pena revisar antes de
  celebrar.

Tambien miramos:

- `train_test_correlation_total_return`: si los ranks de folds en train y
  test estan correlacionados, los buenos folds en train tienden a ser buenos
  en test.
- `test_worst_total_return`: el peor fold realista. Si tu negocio no soporta
  esa caida, el seteo no es viable aunque la media luzca bien.

## Archivos generados

Bajo `--output_dir`:

| Archivo | Contenido |
|---|---|
| `walk_forward_report.md` | Resumen humano: configuracion, agregados, tabla de folds, veredicto. |
| `fold_summary.csv` | Detalle por fold: `fold_index`, ventanas, `train_run_id`, `test_run_id`, `train_total_return`, `test_total_return`, `test_sharpe`. |

Cada fold tambien deja artefactos completos del run, como cualquier backtest
estandar:

| Ruta | Contenido |
|---|---|
| `data/events/run_<run_id>/...` | Eventos del run (Parquet, modo `lite`). |
| `data/equity/run_<run_id>/equity.parquet` | Curva de capital del run. |
| `data/checkpoints/run_<run_id>/...` | Checkpoints (cuando Fase 2 los habilite). |

## Wrap del helper legacy

`backtest/walkforward.py` (split simple en dos partes) sigue presente y
soportado. La nueva ruta multi-fold vive en `backtest/walkforward_runner.py`
y se apoya en `backtest/aggregator.py::aggregate_walk_forward_metrics` para
producir el dict consolidado.
