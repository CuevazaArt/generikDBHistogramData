# Backtesting Tester — Capacidades

Reporte de las capacidades actuales del backtester y de la terminal `backtest_cli.py`.

> Fecha de revisión: 2026-05-19  
> Entrypoint principal: `python backtest_cli.py --db klines.db menu`

---

## 1. Panorama del repositorio

- **Datos**: histogramas (klines) de Binance almacenados en SQLite (`klines.db`).
- **Ingesta**: `binance_hist_downloader.py` + `cli.py` para vía API y ZIP mensual.
- **API local**: `service.py` (FastAPI) expone consultas/export sobre la DB.
- **Terminal de exploración**: `terminal_ui.py` para inspeccionar la DB.
- **Backtester**: paquete `backtest/` + entrypoint `backtest_cli.py`.
- **Bots importados (referencia)**: carpeta `imported_bots/` con las versiones live de Dorothy, Elphaba, Louise/Anti-Louise. Son la base de los adaptadores en `backtest/strategies.py`.

### Críticas y observaciones

- ✅ Buen aislamiento por módulo (`engine`, `runner`, `strategies`, `optimize`, `plots`, `storage`, `walkforward`).
- ✅ Persistencia exhaustiva en SQLite (runs, eventos, métricas, trials, métricas por trial).
- ✅ Dashboard del menú ya muestra Top 3 setups por bot activo, último run y defaults persistidos.
- ✅ Métricas avanzadas integradas (`sortino`, `calmar`, `ulcer_index`) en el resumen estándar y disponibles como objetivo de Optuna.
- ✅ Walk-forward simple (un solo split train/validación, configurable por porcentaje) accesible desde menú y CLI.
- ✅ Tests automatizados (`pytest`) sobre `metrics`, `broker`, `pecunator_trend` y `registry`.
- ⚠️ `backtest_cli.py` concentra mucha lógica de UX; podría dividirse en `cli/format.py`, `cli/menu.py`, `cli/commands.py` si crece más.
- ⚠️ `thusnelda` queda como placeholder porque conceptualmente es multi-activo y el engine actual es mono-activo. Para adaptarla se requiere un engine extendido (lista de símbolos + tabla de posiciones por activo + correlaciones).

---

## 2. Capacidades del engine (`backtest/engine.py`)

| Capacidad | Detalle |
|---|---|
| Datos | Carga directa desde SQLite con filtrado `symbol`, `interval`, `start_ts`, `end_ts`. |
| Velas | Transformación opcional Heikin-Ashi (`use_heikin_ashi`) y elección de `price_source`. |
| Indicadores | SMA fast/slow, EMA, RSI, ATR aplicados por defecto. SMAs custom para `sma_cross`. |
| Broker | `SpotBroker` con `fee_rate` y `slippage_bps` configurables, market orders. |
| Métricas | `total_return`, `final_equity`, `max_drawdown`, `sharpe`, `sortino`, `calmar`, `ulcer_index`, `win_rate`, `profit_factor`, `num_trades`. |
| Eventos | Cada barra produce un evento (`fill`, `order_rejected`, `hold`) con payload JSON. |
| Persistencia | Todos los eventos, métricas y configuración del run quedan en SQLite. |

---

## 3. Estrategias / Bots disponibles

| Bot | Tipo | Descripción breve | Estado |
|---|---|---|---|
| `sma_cross` | Cruce de medias | Compra en golden cross, vende en death cross. | ✅ Operativo |
| `dorothy` | DCA largo + Pecunator gate | DCA con anclas de venta y trigger de caída sobre la mejor ancla; usa el gate de tendencia HA. | ✅ Operativo |
| `dorothy_legacy` | DCA largo histórico | Versión original de Dorothy con `min/max_order_notional` y `max_active_orders`. | ✅ Operativo |
| `elphaba` | DCA inverso + Pecunator gate | Versión bearish: DCA en subidas, cubre con caídas relativas al ancla. | ✅ Operativo |
| `ha_trend` | Trend HA | Trade sobre cruce HA con modo `both/long/short`. | ✅ Operativo |
| `masha` | Tendencia + pullback | SMA fast/slow + entrada por pullback contra fast + TP/SL %. | ✅ Operativo |
| `louise` | DCA bajista | DCA con `target_profit_pct` sobre `avg_entry` + drop relativo al último fill. | ✅ Operativo |
| `louise_lucky` | Louise + lucky | Louise + entrada extra cuando el precio toca `ha_low` previo. | ✅ Operativo |
| `anti_louise` | DCA inverso (spot) | Mirror de Louise: DCA en subida + cobertura por caída relativa al `avg_entry`. | ✅ Operativo |
| `anti_louise_lucky` | Anti-Louise + lucky | Variante que añade entrada por toque de `ha_high` previo. | ✅ Operativo |
| `thusnelda` | Multi-activo | Placeholder; el engine actual es mono-activo. | ⏸️ Placeholder |

Hub: cada bot puede ejecutarse en múltiples instancias con setups distintos. En backtest cada instancia es un run independiente.

---

## 4. Capacidades de Optuna (`backtest/optimize.py`)

| Capacidad | Estado |
|---|---|
| Persistencia en SQLite (`bt_trials`, `bt_trial_metrics`) | ✅ |
| Reanudación de estudios (`load_if_exists=True`) | ✅ |
| Sampler configurable (`tpe`, `random`) | ✅ |
| Semilla determinística (`--seed`) | ✅ |
| Métrica objetivo configurable | ✅ — `total_return`, `final_equity`, `sharpe`, `sortino`, `calmar`, `ulcer_index`, `profit_factor`, `win_rate`, `max_drawdown`, `num_trades` |
| Dirección configurable | ✅ — `maximize` / `minimize` |
| Rango custom por hiperparámetro | ✅ — vía menú o flags `--*_min`/`--*_max` |
| Trials inválidos podados | ✅ — ej. `fast >= slow` en `sma_cross`/`masha` |
| Paralelismo (`n_jobs`) | ✅ |
| Timeout opcional | ✅ |
| Reporte automático del estudio (md/csv/json + gráfica) | ✅ |

### Hiperparámetros por bot (vista rápida)

- **dorothy / dorothy_legacy**: `profit_factor`, `margin_drop_factor`, `max_rungs`.
- **elphaba**: `profit_factor`, `margin_rise_factor`, `max_rungs`.
- **sma_cross**: `fast`, `slow`.
- **masha**: `fast`, `slow`, `take_profit_pct`, `stop_loss_pct`, `pullback_factor`.
- **louise / louise_lucky**: `target_profit_pct`, `margin_drop_factor` (+ `lucky_window` si lucky).
- **anti_louise / anti_louise_lucky**: `target_profit_pct`, `margin_rise_factor` (+ `lucky_window` si lucky).
- **ha_trend**: `trend_mode` (`both|long|short`).

---

## 5. Interfaz / accesibilidad

Mejoras vigentes en `backtest_cli.py`:

- Tablas ASCII con bordes y alineación por columna para todos los listados.
- Banners de encabezado y separadores por sección.
- Formateo consistente:
  - Porcentajes (`%`) para `total_return`, `max_drawdown`, `win_rate`.
  - Miles para `initial_cash`, `final_equity`.
- Defaults persistidos en `.backtest_menu_settings.json`:
  - `last_symbol`, `last_interval`, `last_study`, `last_trials`, `last_n_jobs`.
  - `last_initial_cash`, `last_fee_rate`, `last_slippage_bps`.
  - `last_objective_metric`, `last_direction`, `last_sampler`, `last_seed`.
- Dashboard del menú incluye:
  - Tabla de bots con `activo`, `ultimo_test` y `mejor_return`.
  - Top 3 setups del bot activo con preview de params.
  - Último run y defaults persistidos.
- Prompts validados con reintento amistoso (`int`, `float`, choices).

---

## 6. Reportes generados

| Origen | Archivos |
|---|---|
| `plot --run_id X` | `run_X_equity.png`, `run_X_drawdown.png`, `run_X_returns_hist.png`, `run_X_trade_signal_hist.png`, `run_X_signal_activation_hist.png`, `run_X_metrics.json`, `run_X_equity.csv`, `run_X_report.json` |
| `plot --study NAME` | `study_NAME_trials.png`, `study_NAME_summary.{json,md,csv}` |
| `optimize` | Trials persistidos en `bt_trials` + métricas en `bt_trial_metrics`. |
| `run` | Eventos en `bt_events` + métricas en `bt_metrics`. |

---

## 7. Comandos clave

```bash
# Menú interactivo (recomendado)
python backtest_cli.py --db klines.db menu

# Run directo
python backtest_cli.py --db klines.db run --strategy louise_lucky --symbol BTCUSDT --interval 1h

# Optimización con métrica y sampler configurables
python backtest_cli.py --db klines.db optimize \
  --strategy masha --symbol BTCUSDT --interval 1h \
  --study masha_sharpe --trials 50 --n_jobs 2 \
  --objective_metric sharpe --direction maximize \
  --sampler random --seed 42

# Walk-forward (split train/validacion)
python backtest_cli.py --db klines.db walkforward \
  --strategy masha --symbol BTCUSDT --interval 1h \
  --study masha_wf --trials 20 --train_pct 0.7 \
  --objective_metric sharpe

# Inspección
python backtest_cli.py --db klines.db show --limit 10
python backtest_cli.py --db klines.db show --study masha_sharpe
python backtest_cli.py --db klines.db plot --run_id 46
python backtest_cli.py --db klines.db plot --study masha_sharpe

# Tests
python -m pytest tests -q
```

---

## 8. Glosario de métricas

- **total_return**: retorno total acumulado sobre el capital inicial.
- **max_drawdown**: peor caída desde un pico hasta su siguiente valle (en %).
- **sharpe**: rendimiento medio dividido por volatilidad total. Premia retornos suaves.
- **sortino**: como Sharpe pero solo penaliza la volatilidad a la baja. Castiga menos las subidas grandes.
- **calmar**: `total_return / max_drawdown`. Cuánto rinde por unidad de máxima caída soportada.
- **ulcer_index**: cuánto "duele" estar en drawdown a lo largo del tiempo, no solo el peor punto.
- **win_rate**: porcentaje de trades cerrados con ganancia.
- **profit_factor**: ganancia bruta dividida por pérdida bruta.
- **num_trades**: cantidad de trades cerrados.

Las primeras tres adicionales (`sortino`, `calmar`, `ulcer_index`) ayudan a distinguir setups que parecen iguales por `total_return` pero tienen perfiles de riesgo muy distintos.

---

## 9. Walk-forward, en una línea

`Walk-forward` divide tu histórico en dos: usa el primer `train_pct` para optimizar y el resto para validar el setup ganador. Si el resultado en validación se parece al de train, el setup es razonable; si cae mucho, es sobreajuste.

---

## 10. Próximos pasos sugeridos

1. **Pruner Optuna** (`MedianPruner`/`Hyperband`) para abortar trials malos.
2. **Multi-objective Optuna** (NSGA-II) para optimizar p.ej. `sharpe` y `max_drawdown` a la vez.
3. **Engine multi-activo** para destrabar `thusnelda` y hubs reales.
4. **Walk-forward multi-fold** (varios train/validation rodantes) para honestidad estadística mayor.
5. **Modularizar `backtest_cli.py`** en `cli/format.py`, `cli/menu.py`, `cli/commands.py` para reducir su tamaño.
