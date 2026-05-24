# agartha — Moonshot trailing para Binance Alpha

Bot **independiente y especializado** para operar tokens del mercado
**Binance Alpha** (semilla, memecoins, alta volatilidad).

Implementacion: `backtest.strategies.AgarthaStrategy`. Registry: `agartha`.

---

## Tesis de inversion

> Comprar una posicion **pequena y medida** (capital de riesgo, ej. 10 USDT)
> en un simbolo Alpha; **dejarla correr** sin stop-loss; proteger el beneficio
> con un **trailing stop dinamico**; aceptar perdidas asimetricas a cambio de
> capturar eventos de **x5 / x10 / x20**.
>
> El despliegue es **N instancias en N simbolos**: la cartera apuesta a que
> uno (o pocos) **moonshot** pague todos los fiascos.

### Por que sin stop-loss

- En Alpha, los drawdowns iniciales **no son senial** (precio caotico).
- Un SL convencional **cortaria** la posicion antes de la fase de pump.
- El trailing **solo se activa** cuando hay ganancia (configurable) y
  vende **desde el peak**, no desde el entry.

### Por que trailing

- El upside esperado es **asimetrico**: pocas posiciones generan el grueso.
- Salir en el primer rebote arruina el sesgo; salir tarde devuelve ganancia.
- Trailing % deja correr lo que sube y corta lo que devuelve.

---

## Logica del bot (`on_bar`)

```
1. Sin posicion + sin cash → hold
2. Sin posicion + cash >= notional →
   - Si cycles_closed > 0 y no allow_reentry → hold (single-shot)
   - Si no → BUY notional fijo, ancla entry_price = fill_price
3. En posicion:
   a. bars_in_position += 1; peak_price = max(peak, price)
   b. Time stop: bars_in_position >= max_holding_bars → SELL 100%
   c. Activacion trailing: si activation_profit_pct > 0,
      activar solo cuando price >= entry*(1 + activation_profit_pct/100)
   d. Partial TP (opcional, una vez): si price >= entry*(1+partial_tp_pct/100)
      → SELL partial_tp_size_pct
   e. Trailing: floor = peak*(1 - trailing_stop_pct/100)
      - Si breakeven_lock_pct > 0 y peak >= entry*(1+breakeven_lock_pct/100):
        floor = max(floor, entry_price)   # nunca pierdes capital
      - Si price <= floor → SELL 100%
   f. Resto → hold
```

### Estado interno (serializable)

| Campo | Tipo | Rol |
|---|---|---|
| `entry_price` | float | precio del fill inicial |
| `peak_price` | float | maximo desde la compra |
| `bars_in_position` | int | contador para time stop |
| `trailing_active` | bool | gate de activacion del trailing |
| `partial_tp_done` | bool | el TP parcial ya disparo una vez |
| `cycles_closed` | int | ciclos cerrados (single-shot vs re-entry) |

Persistido via `export_state` / `import_state` para cadenas mensuales /
checkpoints / resume.

---

## Parametros (CLI + manifest)

| Parametro | Default | Rol |
|---|---:|---|
| **`quote_order_qty_usdt`** | 10.0 | Capital de riesgo por instancia (USDT) |
| **`trailing_stop_pct`** | 30.0 | % de retroceso desde el peak para vender |
| `activation_profit_pct` | 0.0 | % de ganancia minima para activar trailing (0=desde inicio) |
| `max_holding_bars` | 0 | Time stop en barras (0=sin limite) |
| `breakeven_lock_pct` | 0.0 | Cuando peak >= entry*(1+x/100), trailing nunca baja del entry |
| `partial_tp_pct` | 0.0 | % sobre entry para TP parcial (0=off) |
| `partial_tp_size_pct` | 0.0 | Fraccion (0..1) a vender en TP parcial |
| `allow_reentry` | false | Permitir re-entrada tras cierre (default single-shot) |

### Search space (optuna)

| Param | Min | Max |
|---|---:|---:|
| `trailing_stop_pct` | 10.0 | 60.0 |
| `activation_profit_pct` | 0.0 | 50.0 |
| `breakeven_lock_pct` | 0.0 | 30.0 |

`quote_order_qty_usdt` queda **fijo** (definicion de capital de riesgo del
operador, no de optimizacion).

---

## Presets registrados

| Preset | Idea |
|---|---|
| `default` | Trailing 30% puro; activo desde la primera vela |
| `moonshot_protected` | Trailing 35%, activacion en +50%, breakeven lock en +100% |
| `partial_then_moon` | Recupera capital a 3x (vende 35%), deja correr el resto con trailing 50% |

---

## Despliegue en cartera

Cada **`Agartha`** opera **un (1) simbolo**. La cartera Alpha se construye
desplegando **N instancias en N simbolos**:

```
Capital total = N * quote_order_qty_usdt
Riesgo maximo por instancia = quote_order_qty_usdt (peor caso: token a 0)
Upside cartera = sum(payouts moonshots) - sum(fracasos)
```

Ejemplo: 20 instancias x 10 USDT = 200 USDT en riesgo.
- Si 1 instancia hace x10 con trailing capturando 5x neto → +40 USDT (4x sobre los 10 iniciales)
- 19 fracasos a 0 → -190 USDT
- **Neto: -150 USDT** → el modelo requiere o **mayor hit rate** o **upsides mas grandes**

Implicacion practica: el seteo del trailing debe **dejar correr** lo suficiente
para que un x20 capturado a x12 amortice 19 ceros (+110 USDT vs -190 = aun
negativo). Sin **multiples** moonshots o **screening** del universo, la
matematica es exigente.

---

## Filtros del mercado Alpha (a respetar a nivel ejecucion)

- **`orderTypes: ["LIMIT"]`** — no MARKET en Alpha trade
- **`PERCENT_PRICE_BY_SIDE`** — bid hasta 5x arriba, 0.2x abajo: en crash las
  ventas limit pueden quedar **fuera de banda**
- **`MIN_NOTIONAL`** muy bajo (0.1 USDT) — 10 USDT pasa con holgura
- Tokens **`offline`** / **`offsell`** — excluir del universo via token list

---

## Accesorios futuros (extensiones planeadas)

| Accesorio | Idea |
|---|---|
| **ATR trailing** | Reemplazar `trailing_stop_pct` por `k * ATR(period)` adaptativo |
| **Volume gate** | No comprar si `volume24h / liquidity` debajo de umbral |
| **Holder gate** | Filtro min `holders` desde token list |
| **Listing gate** | Solo entrar si listing < N dias |
| **Multi-rung partial** | Escalera de TPs parciales (25% a 2x, 25% a 5x, resto trailing) |
| **Re-entry inteligente** | Tras cierre, re-comprar si precio re-toca `breakeven_lock_pct` |
| **Cross-bot delisting hook** | Forzar cierre si token marcado `offline` en next refresh |

Cada accesorio se anadiria como **flag opcional** sin romper la tesis base.

---

## CLI

Run unico:

```powershell
$env:PYTHONPATH='.'
python backtest_cli.py --db klines.db run `
  --strategy agartha --symbol ALPHA_175USDT --interval 1m `
  --start_ts 1750000000000 --end_ts 1760000000000 `
  --initial_cash 100 --quote_order_qty_usdt 10 `
  --trailing_stop_pct 30 --activation_profit_pct 50 `
  --breakeven_lock_pct 100
```

Optimize (barrido de trailing/activacion):

```powershell
python backtest_cli.py --db klines.db optimize `
  --strategy agartha --symbol ALPHA_175USDT --interval 1m `
  --study agartha_alpha175 --trials 40 `
  --start_ts 1750000000000 --end_ts 1760000000000
```

---

## Pendientes

- [ ] Conector live REST/WS Alpha (autenticacion, LIMIT-only, PERCENT_PRICE)
- [ ] Orchestrator multi-instancia (cartera de N Agarthas en paralelo)
- [ ] Screening del universo (token list + filtros liquidez/holders)
- [ ] Backtests reales sobre datos Alpha descargados (`cli.py --mode alpha_api`)
- [ ] Runs registry con metricas tipicas: hit rate, payout promedio, ratio Sharpe
- [ ] Accesorio ATR trailing y volume gate
