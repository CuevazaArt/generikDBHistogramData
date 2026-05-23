# dorothy

Adaptador de backtest para Pecunator Dorothy: DCA con escalera de profit
operando exclusivamente cuando el gate de tendencia HA está BULLISH **si
`require_trend_gate=True`**. Desde 2026-05-23 el backtest strict deja los gates
**desactivados por defecto** (`--require-trend-gate` para activar gate 1;
`--require-entry-gate` para gate 2 live parity).

Registro comparativo reproducible: [`runs_registry.md`](runs_registry.md).

## Tesis

- En spot long-only, comprar de a tramos cuando el precio cae por debajo
  de un anclaje configurable y descargar mediante límites de venta cuando
  el precio alcanza objetivos de profit acumulado.
- Filtrar entradas con `pec_trend == "BULLISH"` cuando gate 1 está activo
  (`--require-trend-gate`). **Default backtest strict: gates OFF.**

## Decisiones

- `profit_factor`: ganancia objetivo por límite de venta (se compone con
  la caída adicional `margin_drop_factor` para gatillar la siguiente
  compra).
- `max_rungs`: número máximo de niveles activos simultáneos. Detiene
  nuevas compras cuando se alcanza para limitar exposición.
- Las salidas son solo por límites: un trigger `sell_limit_hit` empuja la
  proporción de tramos cubiertos.
- El estado serializable (`active_sell_limits`) habilita warm restarts
  entre runs sin perder los anclajes activos.

## Observaciones

- Sensible al spread `profit_factor + margin_drop_factor`. Valores muy
  cercanos a 0 producen frecuencia altísima de compras con baja prima por
  tramo (impacto de fees).
- En `XRPUSDT 1h` el sweet spot histórico oscila en
  `profit_factor∈[0.04, 0.06]` y `margin_drop_factor∈[0.005, 0.015]`.
- Alias `dorothy_hub` se mantiene por compatibilidad con scripts
  anteriores.

## Runs XRPUSDT 1s (2024) — notas de campo

Corridas con `scripts/run_xrpusdt_2024_dorothy_strict.py`, cadena **mensual
encadenada** (`2024-01`→`2024-12` en el formato actual; las corridas
historicas usaron etiquetas legacy `M01..M12`), estado broker +
`active_sell_limits` heredado entre meses, datos en `klines.db` + Parquet
`data/klines/symbol=XRPUSDT/interval=1s/year=2024/` (~31,6M velas). Desde
2026-05 las ventanas se generan dinamicamente desde `--start_ts/--end_ts`
(via `backtest.calendar_windows.monthly_windows`), por lo que la misma
receta aplica a 2025 u otros simbolos/intervalos. Seteo común salvo donde
se indica:

| Parámetro | Valor |
| --- | --- |
| `initial_cash` | 1000 USDT |
| `loop_seconds` | 29 |
| `quote_order_qty_usdt` | 8 |
| `fee_rate` / `slippage_bps` | 0.1 % / 2 |
| `events_mode` | `lite` (`snapshot_seconds=3600`) |
| `max_rungs` | 0 (sin tope; ver decisión en código) |
| `symbol` / `interval` | XRPUSDT / 1s |
| Ventana | 2024-01-01 … 2024-12-31 (ms en manifests Parquet) |

### Resultados resumidos

| Corrida | `profit_factor` | `margin_drop_factor` | Equity final | Notas |
| --- | --- | --- | --- | --- |
| Solo enero 2025-05-22 | 0.05 | 0.0005 | **977** (−2,3 % vs 1000) | `run_id=4`; 10 trades |
| Feb–Dic desde estado ene (`seed-run-id 4`) | 0.05 | 0.0005 | **1588** | 11 meses; misma trayectoria que año con cap rungs |
| Año completo, rungs acotados (~125) | 0.05 | 0.0005 | **1588** | `run_id` 16–27 (dic); ~65 límites activos al cierre |
| Año completo, **sin tope rungs** | 0.05 | 0.0005 | **1588** | Idéntico a la fila anterior en equity: el cap no fue el binding |
| Año completo (mejor equity en la serie) | **0.02** | **0.0003** | **1860** (+86 % vs 1000) | `run_id` 28–39; ~155 límites; ene **920**; más DCA/rotación |
| Año completo, TP más ancho | **0.10** | 0.0003 | **1241** (+24 % vs 1000) | `run_id` 40–51; ~23 límites; ~1006 cash / ~113 XRP al cierre; ene **990**, 2 trades |

Informes bajo `reports/entregables/strict/` (prefijo
`dorothy_xrpusdt_1s_monthly_chain_YYYYMMDD_HHMMSS/`).

### Conclusiones operativas

1. **¿Da dinero en este backtest?** En XRP 2024 (contexto alcista fuerte),
   sí en **equity de marcado** con `profit_factor=0.02` y `margin_drop_factor=0.0003`.
   Con `profit_factor=0.10` gana menos (+24 %) porque vende menos (TP más lejos).
   Con `0.05` intermedio (~+59 % vs 1000 en la corrida encadenada completa).

2. **¿Es “seguro”?** Solo en el sentido **spot sin liquidación**. No es bajo
   riesgo: enero puede cerrar negativo; drawdowns por mes del orden de **9–31 %**
   en tramos; al cierre suele haber **poca cash** y **mucha posición XRP** con
   decenas de límites abiertos (riesgo de bolsa si el precio cae sin alcanzar TP).

3. **Costo–beneficio**
   - A favor: en este año/par, el DCA encadenado supera variantes con TP muy
     amplio (`pf=0.1`); el estado serializable permite continuidad mensual realista.
   - En contra: muchas operaciones → fees; carga RAM ~1,3 GB/mes en engine Python
     (carga lista completa por mes); **no paralelizar meses** dentro de una cadena;
     generalización **no probada** (un símbolo, un año, params barajados).

4. **Infraestructura**
   - Cadena **trimestral** (~8M velas/carga) en máquina 16 GB con IDE abierto
     **abortó por RAM** (>80 %). Cadena **mensual** (~2,7M velas) estable (~50–90 s/mes,
     ~15–22 min/año).
   - `max_rungs=0` no cambió equity vs cap ~125 en `pf=0.05`: el límite efectivo
     fue **cash** (`quote_order_qty_usdt=8`), no el tope de escalones.

5. **Parámetros (hipótesis para 1s / 2024 XRP)**
   - `profit_factor` bajo (0.02) + `margin_drop_factor` bajo (0.0003) → más
     compras y más takes parciales; mejor captura del rally.
   - `profit_factor` alto (0.10) → menos rotación, más cash ocioso, peor en tendencia
     alcista fuerte.
   - Los rangos citados para **1h** (`0.04–0.06` / `0.005–0.015`) **no trasladan**
     directo a 1s; hace falta re-optimizar o walk-forward en 1s.

### VolumenIncremental (reservado, experimental)

Accesorio opcional en `DorothyHubStrategy`: si `cash` disponible para compra es **mayor**
que `initial_run_cash` de la corrida, la siguiente orden usa
`quote_order_qty_usdt * multiplier`; si no, notional base. Mutuamente excluyente con
VolumenCompuesto. CLI strict: `--volumen-incremental`.

### VolumenCompuesto (experimental, 2026-05-23)

Accesorio alternativo de sizing (no combinar con VI). Usa `Decimal` end-to-end en
`backtest/dorothy_accessories.py`:

- `factor = (equity / initial_equity) * (1 + greed_factor)` (1000→1.0, 1100→1.1, 900→0.9 sin greed)
- `notional = quote_order_qty_usdt * factor` (base 8 USDT)
- **Piso:** 6 USDT (`--volumen-compuesto-min-usdt`)
- **Greed (opcional):** `--volumen-compuesto-greed-factor 0.01` añade +1 % al factor
  (1100 equity → `1.1 * 1.01 = 1.111` → 8.888 USDT). Default `0` = sin boost.

CLI strict: `--volumen-compuesto` [`--volumen-compuesto-greed-factor 0.01`].

### Pendientes / no concluido aquí

- Walk-forward formal (train/test por año) antes de escalar capital.
- Engine **streaming** (sin materializar mes entero) para año completo en <16 GB
  con IDE abierto.
- Validación en vivo (latencia, fills, drift del gate `pec_trend`).
- Flag automático **send-to-Earn** cuando N meses sin trades o BE distance > umbral.

## Filosofía de inversión (2026-05-23)

El activo operado con Dorothy se **pre-selecciona para acumular y holdear** a
3–5 años. Spot sin apalancamiento: drawdown alto **no implica liquidación** ni
venta forzada. Si la bag queda underwater, se registra el **break-even**
(`avg_entry`) y el activo puede pasar a **Earn** (renta pasiva) mientras se
espera un bull market de mediano/largo plazo. El capital desplegado es capital
**dispuesto a perder**; el peor caso aceptable es holdear la bag acumulada.

Bajo este marco, el criterio de éxito **no es maximizar USDT** sino:

- acumular cantidad del activo a precio promedio razonable;
- cristalizar ganancias parciales en cash durante la trayectoria;
- graduar bags estancadas a HODL+Earn sin reset destructivo del encadenado.

## Conclusiones cross-symbol (2024–2026, cadena mensual encadenada)

Seteo base paradigmático (punto de partida, **no universal**):
`VC min=6`, `greed=0.1`, gates OFF, `mdf=0.0005`, `initial_cash=1000`,
`loop_seconds=29`. Ajustar `profit_factor` por activo según análisis.

| Par | pf | Tramo | final USDT | Retorno | Notas |
|---|---:|---|---:|---:|---|
| XRP | 0.02 | 2024 solo | 2022 | +102 % | Techo en bull; no generaliza |
| XRP | 0.02 | 2025 solo | 849 | −15 % | Año malo |
| XRP | 0.02 | 2025–2026 cad. | 580 | −42 % | Bag atrapada; inaceptable sin filosofía HODL |
| XRP | 0.1 | 2024–2026 cad. | 1396 | +40 % | Robusto encadenado |
| BNB | 0.02 | 2024–2026 cad. | 1350 | +35 % | Buen acumulador |
| BNB | 0.1 | 2024–2026 cad. | 1139 | +14 % | Menos trades, DD bajo |
| BTC | 0.03 | 2024–2026 cad. | 1415 | +42 % | Mejor resultado encadenado |
| ETH | 0.03 | 2024–2026 cad. | 948 | −5 % | 2026 destruye; bag underwater |

### Ajustes sugeridos por activo (sobre la base)

| Activo | pf sugerido | Razón |
|---|---:|---|
| BTC | 0.03 | Movimientos amplios; BE distance ~−28 % aceptable |
| ETH | 0.03–0.05 | Volatilidad intermedia; validar 2026 aparte |
| BNB | 0.02–0.05 | Buena acumulación de qty; Earn nativo |
| XRP | 0.1 | Alta volatilidad; pf alto evita over-trading |

### Accesorios y gates

- **VolumenCompuesto** supera a **VolumenIncremental** en bull (2024 XRP).
- **GreedFactor 0.1** aporta +1–2 pp en bull; moderado, no radical.
- **Gates OFF** mejor en bull; en bear la diferencia es marginal con pf alto.
- VI y VC son **mutuamente excluyentes**.

### Dorothy vs HODL puro

En bull fuerte, HODL gana en USDT (XRP 2024: HODL +238 % vs Dorothy +102 %).
Dorothy aporta **rotación parcial** (cash + bag), **avg_entry trazable** y
**checkpoints anuales** (`YEAR_CHECKPOINTS.md`, `year_checkpoint_*.json`).
En lateral/bajista (ETH 24–26) Dorothy puede superar HODL en USDT.

### Veredicto producción (bajo filosofía HODL+Earn)

**Usable en producción** cuando:

1. El activo ya está seleccionado para tenencia 3–5 años.
2. `pf` se calibra por par (no hay seteo universal).
3. La cadena mensual persiste estado entre meses/años.
4. Bags estancadas se gradúan a Earn (manual o vía flag futuro).
5. El capital es “a riesgo” y el peor caso es holdear la bag.

Registro comparativo de corridas: [`runs_registry.md`](runs_registry.md).

Datasets curados: [`reports/entregables/datasets/INDEX.md`](../../../reports/entregables/datasets/INDEX.md).
