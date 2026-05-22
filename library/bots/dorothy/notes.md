# dorothy

Adaptador de backtest para Pecunator Dorothy: DCA con escalera de profit
operando exclusivamente cuando el gate de tendencia HA está BULLISH.

## Tesis

- En spot long-only, comprar de a tramos cuando el precio cae por debajo
  de un anclaje configurable y descargar mediante límites de venta cuando
  el precio alcanza objetivos de profit acumulado.
- Filtrar entradas con `pec_trend == "BULLISH"` reduce DCAs prolongados
  durante mercados bajistas extendidos.

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
encadenada** (M01→M12, estado broker + `active_sell_limits` heredado), datos en
`klines.db` + Parquet `data/klines/symbol=XRPUSDT/interval=1s/year=2024/`
(~31,6M velas). Seteo común salvo donde se indica:

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

### Pendientes / no concluido aquí

- Comparar contra **buy & hold** XRP 2024 en la misma ventana.
- Walk-forward u otro año (2023, 2025) antes de capital real.
- Engine **streaming** (sin materializar mes entero) para año completo en <16 GB
  con IDE abierto.
- Validación en vivo (latencia, fills, drift del gate `pec_trend`).
