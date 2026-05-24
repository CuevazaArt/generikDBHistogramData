# louise

Estrategia DCA en bajada con cierre completo al alcanzar un porcentaje
objetivo sobre el precio promedio de entrada.

## Tesis (modo default)

- En activos con drawdowns frecuentes pero retornos positivos, una DCA
  controlada por `margin_drop_factor` baja el costo promedio rápido y
  permite cerrar al primer rebote modesto (`target_profit_pct`).

## Decisiones

- Sale 100% de la posición en cuanto el precio cruza el TP (si
  `target_profit_pct > 0`). No usa ladders de venta como Dorothy.
- Compra inicial cuando hay cash disponible y no hay posición previa
  o no se conoce el último precio de compra.
- Compras sucesivas (`louise_dca_drop`) sólo cuando el precio cae más
  allá de `last_purchase_price * (1 - margin_drop_factor)`.
- `target_profit_pct <= 0` desactiva ventas por TP (modo acumulación).

## Observaciones

- Sensible a `target_profit_pct` bajos (<0.5): muchísimas operaciones
  pequeñas, fees comen el retorno.
- Funciona bien como bloque base de la familia `louise_lucky` (bot
  independiente con mecánica Lucky Strike; ver `library/bots/louise_lucky/`).

---

## Instrumento HODL+Earn (2026-05-24)

Modo operativo derivado del diálogo de backtests encadenados 2024–2026.
**No es un bot distinto**: es un preset + marco de evaluación sobre
`LouiseStrategy` cuando el activo ya está pre-seleccionado para tenencia
3–5 años (filosofía alineada con Dorothy HODL+Earn, ver
`library/bots/dorothy/notes.md`).

### Premisas

1. El activo se elige **antes** de operar; no hay rotación de par.
2. Spot sin apalancamiento: drawdown alto **no implica liquidación**.
3. Si la bag queda underwater, se registra `avg_entry` y el activo puede
   ir a **Earn** (renta pasiva) mientras se espera remonte.
4. El capital desplegado es capital **dispuesto a holdear**; el peor caso
   aceptable es acumular la bag.
5. El criterio de éxito **no es maximizar USDT** sino: qty acumulada,
   precio promedio razonable, y equity mark-to-market cuando el activo
   remonta — no ciclos de micro-TP.

### Preset registrado

Archivo: [`presets/hodl_earn_accumulate.yaml`](presets/hodl_earn_accumulate.yaml)

| Parámetro | Valor | Rol |
|---|---:|---|
| `target_profit_pct` | **0** | Sin ventas; acumulación pura |
| `margin_drop_factor` | **0.04** | DCA cada ~4 % desde última compra |
| `quote_order_qty_usdt` | **8** | Lote fijo (baja exigencia de capital) |

Motor recomendado (CLI / script piloto):

| Flag | Valor |
|---|---|
| `loop_seconds` | 29 |
| `initial_cash` | 1000 USDT |
| `chain_by_month` | true |
| `profit_target_usdt` | 200 (opcional, corte por objetivo M2M) |

### Diferencia vs Dorothy bajo el mismo espíritu

| | Louise HODL+Earn | Dorothy HODL+Earn |
|---|---|---|
| Ventas | Ninguna (bag intacta) | Parciales por rung |
| Utilización capital | Baja (~5–9 % avg) | Alta (grilla + VC) |
| Sizing | Fijo 8 USDT | VC/VI escalable |
| Mejor cuando | Quieres bag pura + mucho cash idle | Quieres rotación parcial + más qty |

### Corridas de referencia (sin TP, mdf=0.04, loop=29)

| Par | Meses | Beneficio | Objetivo +200 | Notas |
|---|---:|---:|---|---|
| XRP | 11/29 | **+230 USDT** | Sí | Remonte sobre bag acumulada |
| BTC | 29/29 | +25 USDT | No | ~5 % util. capital |
| BNB | 29/29 | +22 USDT | No | ~4 % util. capital |
| ETH | 29/29 | +15 USDT | No | Pico +150, devolvió |

Entregables: `reports/entregables/strict/louise_*_chain_20260524_*`

### Cuándo usar este instrumento

- Activo con convicción de largo plazo y tolerancia a bag underwater.
- Preferencia por **simplicidad** (sin grilla Dorothy) y **bajo despliegue**
  de capital por tick.
- Aceptación de que la mayor parte del equity puede quedar en **cash idle**
  (Earn USDT) mientras la bag (Earn activo) representa una fracción pequeña
  del total hasta que el mercado remonte.
