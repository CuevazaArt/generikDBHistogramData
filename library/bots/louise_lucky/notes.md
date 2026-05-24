# louise_lucky — Lucky Strike (bot independiente)

Bot **long-only spot** de acumulación en bajada con una capa adicional:
**Lucky Strike** — compras oportunistas cuando el precio toca un extremo
inferior local, **sin alterar** el ritmo de la DCA principal.

Implementación: `LouiseLuckyStrategy` (`backtest.strategies`). Registry:
`louise_lucky`.

---

## Qué es Lucky

**Lucky Strike** no es un flag opcional sobre Louise: es la **mecánica
distintiva** de este bot. Detecta momentos en que el precio está en (o por
debajo de) un **mínimo local reciente** y ejecuta una compra extra de
`quote_order_qty_usdt`, aunque la DCA por `margin_drop_factor` no se haya
disparado todavía.

Objetivo: mejorar el **costo promedio** acumulando en puntos de máxima
presión vendedora, sin esperar el umbral rítmico de la DCA.

---

## Filosofía de inversión

### Tesis

En un activo **pre-elegido para holdear**, los mejores momentos para acumular
no son solo “cada X % de caída”, sino también los **puntos de pánico
intradía** — mínimos locales donde el precio toca un suelo relativo antes
de que la DCA rítmica se dispare.

Lucky apuesta a que **comprar en esos suelos mejora el avg_entry** sin
esperar el umbral de `margin_drop_factor`.

### Metáfora operativa

| Capa | Rol | Analogía |
|---|---|---|
| **Louise (base)** | DCA rítmica + salida opcional | Riego programado cada X % de sequía |
| **Lucky** | Strike en mínimos | Abrir la compuerta cuando el río toca su cauce más bajo del tramo |

### Dónde encaja

```
Convicción macro (HODL 3–5 años)
        ↓
Acumulación sistemática (Louise DCA)
        ↓
Refinamiento táctico (Lucky en mínimos)
        ↓
Bag → Earn mientras esperas remonte
```

**Encaja** cuando: activo pre-seleccionado, tesis de acumular qty, mercados
con valles intradía, marco HODL+Earn (ver
[`library/bots/louise/notes.md`](../louise/notes.md) instrumento HODL+Earn).

**No encaja** cuando: buscas mínimas operaciones (usa `louise` puro), tendencia
bajista estructural sin convicción de remonte (Lucky acelera promediar abajo),
o scalping con TP frecuente (Lucky es acumulación, no rotación).

### Relación HODL+Earn

| Modo base | Lucky aporta |
|---|---|
| Con TP | Mejor entrada antes del rebote que dispara venta |
| Sin TP (`target_profit_pct=0`) | Más qty en mínimos → más bag + Earn al remontar |

Preset alineado: [`presets/hodl_earn_accumulate.yaml`](presets/hodl_earn_accumulate.yaml).

---

## Lógica por vela (`on_bar`)

Orden de evaluación:

1. **Louise base** (TP, DCA inicial, DCA por caída) — si devuelve buy/sell,
   se ejecuta y **no** se evalúa Lucky en esa vela.
2. Si la base devuelve **hold** y hay cash ≥ notional:
   - Calcular `lucky_floor`:
     - Preferencia: `ha_low` de la **vela anterior** (Heikin-Ashi).
     - Fallback: mínimo de `price_source` en las últimas `lucky_window` velas.
   - Si `precio <= lucky_floor` → **buy** (`louise_lucky_strike_low`).

```645:668:backtest/strategies.py
    def on_bar(self, ctx: StrategyContext) -> Signal:
        base = super().on_bar(ctx)
        if base.action != "hold":
            return base
        ...
        if price <= lucky_floor:
            return Signal(action="buy", ..., reason="louise_lucky_strike_low", ...)
```

### Regla crítica: anclaje DCA intacto

Las compras Lucky **no actualizan** `last_purchase_price`:

```671:674:backtest/strategies.py
    def on_fill(self, fill, signal: Signal, ctx: StrategyContext) -> None:
        if signal.reason == "louise_lucky_strike_low":
            return
        super().on_fill(fill, signal, ctx)
```

La DCA principal sigue midiendo caídas desde el **último fill “natural”**
(inicial o `louise_dca_drop`), no desde un lucky fill. Así un strike en
un mínimo extremo **no adelanta** el siguiente trigger de DCA.

---

## Parámetros: matriz de independencia

```
┌─────────────────────────────────────────┐
│  LUCKY STRIKE (timing micro)            │
│  Parámetro propio: lucky_window         │
│  Compra en mínimo local; no mueve ancla │
└─────────────────┬───────────────────────┘
                  │ solo si base = hold
┌─────────────────▼───────────────────────┐
│  LOUISE BASE (ritmo + salida)           │
│  Params: mdf, target_profit_pct, quote  │
└─────────────────────────────────────────┘
```

### Parámetro **propio** de Lucky

| Parámetro | Default | Rango | Qué controla |
|---|---:|---|---|
| **`lucky_window`** | 24 | 8 – 72 | Sensibilidad del mínimo local (velas hacia atrás). |

Calibración por intervalo:

| Intervalo | `lucky_window=24` |
|---|---|
| **1h** | ~1 día de contexto |
| **1m** | ~24 minutos |
| **1s** + `loop_seconds=29` | Recalibrar: la ventana es en **velas**, no wall-clock |

Efectos al tunear **solo** `lucky_window`:

- **Bajo (8–12):** mínimos más frescos → más strikes, más agresivo.
- **Alto (48–72):** mínimos más estirados → menos strikes, capitulaciones profundas.

Detección preferente (no parametrizada en backtest): `ha_low` vela anterior.
En live: HA low del **daily cerrado** (micro vs macro).

### Parámetros **heredados** (capa Louise, ortogonales a Lucky)

| Parámetro | Default | Rol | vs Lucky |
|---|---:|---|---|
| `target_profit_pct` | 1.5 | TP 100 % sobre avg | **Ortogonal.** `0` = acumular; Lucky sigue en mínimos. |
| `margin_drop_factor` | 0.004 | Umbral DCA rítmica | **Ortogonal.** Lucky solo actúa en **hold** de la base. |
| `quote_order_qty_usdt` | 8.0 | Notional por compra | **Compartido** (lucky y DCA mismo lote). |

Interacción **`margin_drop_factor` × Lucky**:

| mdf | Efecto |
|---|---|
| Bajo (0.004) | DCA se dispara antes → Lucky compite menos con la base |
| Alto (0.04) | DCA más lenta → más ventanas en hold → **más strikes Lucky** |

Para estudiar **solo Lucky**, fijar mdf/TP/quote y barrer `lucky_window`.

### Motor (no en manifest; calibran Lucky)

| Flag | Efecto |
|---|---|
| `loop_seconds` | Frecuencia de evaluación del strike |
| `interval` | Escala temporal de `lucky_window` y `ha_low` |
| `initial_cash` | Cash compartido entre DCA y lucky fills |

---

## Parámetros editables (resumen CLI)

| Parámetro | Default | Rango Optuna | Función |
|---|---:|---|---|
| `lucky_window` | 24 | 8 – 72 | Velas para el mínimo móvil (fallback si no hay `ha_low`) |
| `target_profit_pct` | 1.5 | 0.2 – 5.0 | TP sobre avg (0 = sin venta) |
| `margin_drop_factor` | 0.004 | 0.001 – 0.03 | Umbral DCA principal |
| `quote_order_qty_usdt` | 8.0 | — | Notional por compra (normal o lucky) |

Presets: [`default.yaml`](presets/default.yaml),
[`hodl_earn_accumulate.yaml`](presets/hodl_earn_accumulate.yaml).

---

## Tres tipos de compra

| Tipo | Reason | Actualiza `last_purchase_price` | Condición |
|---|---|---|---|
| Inicial | `louise_initial_buy` | Sí | Sin posición / sin anclaje |
| DCA | `louise_dca_drop` | Sí | Precio < last × (1 − mdf) |
| **Lucky** | `louise_lucky_strike_low` | **No** | Precio ≤ mínimo local / HA low |

Ventas: igual que Louise — TP 100 % si `target_profit_pct > 0`.

---

## Live vs backtest

| Aspecto | Backtest | Live (`imported_bots/.../louise.py`) |
|---|---|---|
| Detección lucky | `ha_low` vela previa o min(`lucky_window`) | **HA low del último daily cerrado** (1d) |
| Marcado | `reason` en signal | `is_lucky_fill` en metadata WS |
| Efecto anclaje | Igual: lucky no mueve `last_purchase_price` | Igual |

En live el criterio es **más macro** (diario HA); en backtest es **intrabar**
con ventana configurable. Calibrar `lucky_window` por intervalo.

---

## Intervalos y calibración

Manifest declara **1m, 1h**. Baseline documentado:

- `lucky_window=24` en **1h** ≈ 1 día de contexto para el mínimo móvil.
- En **1s** con `loop_seconds=29`, re-calibrar ventana (más velas = más
  contexto temporal).

Si no hay mínimos locales nuevos, el bot se comporta **idéntico a Louise**.

---

## Gates y accesorios

- **Gates Pecunator**: anota `pec_trend` en `on_start`; **no filtra** compras.
- **Sin** VolumenCompuesto / VolumenIncremental / max_rungs.
- Misma baja exigencia de capital que Louise (~8 USDT/tick).

---

## Familia simétrica

| Bot | Lucky en | Extremo |
|---|---|---|
| `louise_lucky` | Mínimos | Long DCA |
| `anti_louise_lucky` | Máximos | Inverse DCA (spot) |

---

## Cuándo usar Lucky vs Louise puro

| Usar **louise_lucky** | Usar **louise** |
|---|---|
| Quieres capturar **capitulaciones locales** antes del umbral DCA | Basta DCA rítmica por caída % |
| El activo hace **picos y valles** intradía | Prefieres menos compras, lógica mínima |
| Aceptas más fills en mínimos (mejor avg potencial) | Quieres menos operaciones |

---

## Pendientes

- Corridas encadenadas 2024–2026 comparables al instrumento HODL+Earn de Louise.
- Alinear criterio live (1d HA) vs backtest (intrabar) en un preset por intervalo.
- Registry de runs (`runs_registry.md`) tras primeros estudios cross-symbol.
