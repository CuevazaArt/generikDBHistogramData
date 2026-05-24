# louise_lucky — Lucky Strike (bot especialista, independiente)

**Lucky** es un bot **discreto e independiente** de la familia Louise/Dorothy.
No es una variante ni un accesorio: tiene **propia lógica**, **propia razón
de inversión** y actúa solo ante una **situación concreta y eventual**.

Registry: `louise_lucky` · Clase: `LouiseLuckyStrategy` (nombre histórico
del adapter; la tesis de producto es autónoma).

---

## Identidad

| Atributo | Lucky |
|---|---|
| Tipo | Bot **especialista**, evento-driven |
| Objetivo | **Una sola cosa:** comprar cuando el precio toca un **mínimo local** |
| Alcance | Oportunidad **táctica** y **acotada**; no acumulación general |
| Relación con otros bots | **Ninguna dependencia operativa.** Puede coexistir en cartera con Louise o Dorothy, pero no los extiende ni los requiere |

---

## La situación concreta que ataja

Lucky existe para un escenario **específico y eventual**:

> El precio, dentro de una ventana reciente, **toca o perfora el suelo local**
> (mínimo relativo / extremo inferior HA) — señal de **capitulación local**
> a corto plazo.

En ese instante y **solo entonces**, ejecuta un strike: compra fija de
`quote_order_qty_usdt` USDT.

**Fuera de ese escenario, Lucky no opina.** No programa DCA, no define ritmo
de acumulación, no gestiona grilla. Si el mercado no presenta mínimos locales
nuevos, el bot **permanece inactivo** (hold).

---

## Razón de inversión

### Tesis

En activos **pre-elegidos para tenencia larga**, existen ventanas breves donde
el precio **sobre-reacciona a la baja** dentro de un tramo reciente. Esos
puntos suelen ofrecer **mejor precio de entrada** que compras mecánicas
espaciadas en el tiempo.

Lucky **no predice** el fondo del ciclo ni el timing macro. Apuesta a una
premisa acotada:

- un **mínimo local** es un evento reconocible;
- comprar en ese evento mejora el **costo marginal** de la posición;
- la convicción de largo plazo (HODL + Earn) hace tolerable holdear si el
  strike no marca el mínimo absoluto.

### Qué NO es

| Lucky **no es** | Por qué |
|---|---|
| Bot DCA general | No compra “cada X %”; espera el **evento** mínimo local |
| Louise con extra | Louise rige **ritmo**; Lucky atiende **evento** |
| Dorothy / grilla | Sin rungs, sin TP parcial, sin VC |
| Market timer macro | No usa tendencia ni gates para decidir |
| Estrategia always-on | Sin mínimos locales → **sin operación** |

### Dónde encaja en una cartera

```
Convicción macro (activo pre-elegido, HODL 3–5 años)
        ↓
[Opcional] Acumulación mecánica (Louise / Dorothy)  ← bots distintos
        ↓
Lucky Strike (especialista: capitulación local)       ← este bot
        ↓
Bag → Earn + espera remonte
```

Lucky encaja como **satélite especializado**: capital y slots dedicados a
capturar **eventos**, no a sustituir la acumulación principal.

---

## Lógica operativa (núcleo Lucky)

**Condición de strike** (backtest):

1. Hay cash ≥ `quote_order_qty_usdt`.
2. `precio <= lucky_floor`, donde `lucky_floor` es:
   - `ha_low` de la vela anterior (preferente), o
   - mínimo de `price_source` en las últimas **`lucky_window`** velas.

**Acción:** BUY fijo (`louise_lucky_strike_low`).

**Regla de diseño:** el fill lucky **no actualiza** anclajes de ritmo DCA
(`last_purchase_price`). El strike es **aislado** del estado de otros bots
o capas.

### Nota de implementación (adapter actual)

El código en `backtest.strategies` evalúa primero una capa Louise heredada
(TP, DCA) y solo en **hold** ejecuta el strike Lucky. Eso es **deuda técnica
de empaquetado**, no la tesis de producto. La identidad canónica de Lucky es
**solo el strike en mínimo local**; la refactorización futura apunta a un
adapter puro evento-only.

---

## Parámetros

### Propios de Lucky (los que definen al especialista)

| Parámetro | Default | Rango | Rol |
|---|---:|---|---|
| **`lucky_window`** | 24 | 8 – 72 | **Único knob de la especialización:** qué tan “local” es el mínimo. |
| `quote_order_qty_usdt` | 8.0 | — | Tamaño del strike (notional por evento). |

Calibración de **`lucky_window`**:

| Valor | Comportamiento |
|---|---|
| **8–12** | Mínimos muy frescos → más eventos, más agresivo |
| **24** (baseline 1h) | ~1 día de contexto |
| **48–72** | Solo capitulaciones más profinas / estiradas |

Detección live: HA low del **daily cerrado** (macro). Backtest: intrabar +
ventana — calibrar por intervalo y `loop_seconds`.

### Parámetros presentes por herencia de adapter (no son la tesis Lucky)

| Parámetro | Nota |
|---|---|
| `target_profit_pct` | Pertenece a capa Louise empaquetada; en tesis pura Lucky **no vende**. Usar `0` en modo acumulación. |
| `margin_drop_factor` | Pertenece a DCA rítmica Louise; **no define** el strike. Ignorar al estudiar Lucky puro. |

Para aislar Lucky en backtests: fijar `target_profit_pct=0`, `margin_drop_factor`
alto (p. ej. 0.04) para minimizar interferencia DCA, y barrer **`lucky_window`**.

Presets:

- [`default.yaml`](presets/default.yaml) — adapter empaquetado (legacy).
- [`hodl_earn_accumulate.yaml`](presets/hodl_earn_accumulate.yaml) — acumulación
  sin TP; strike activo en mínimos.

---

## Live vs backtest

| Aspecto | Backtest | Live |
|---|---|---|
| Evento | `precio <= lucky_floor` intrabar | `precio <= ha_low` daily cerrado |
| Marcado | `louise_lucky_strike_low` | `is_lucky_fill` |
| Anclaje | Strike no mueve `last_purchase_price` | Igual |

---

## Gates y capital

- Sin gates activos (solo anota `pec_trend`).
- Sin accesorios VC/VI / max_rungs.
- Baja exigencia: ~8 USDT por **evento**; capital idle entre strikes.

---

## Simétrico (familia de producto, no dependencia)

| Bot | Evento | Extremo |
|---|---|---|
| **`louise_lucky`** | Mínimo local | Long strike |
| `anti_louise_lucky` | Máximo local | Inverse strike |

---

## Cuándo desplegar Lucky

| Sí | No |
|---|---|
| Activo pre-elegido, convicción largo plazo | Necesitas acumulación mecánica continua → **Louise** |
| Quieres **especialista** en capitulaciones locales | Quieres grilla / rotación parcial → **Dorothy** |
| Aceptas **inactividad** entre eventos | Necesitas bot always-on |

---

## Pendientes

- Adapter **puro** solo-strike (sin capa Louise en `on_bar`).
- Corridas 2024–2026 aislando `lucky_window` vs baseline sin strikes.
- Preset live-aligned (detección 1d HA) vs intrabar backtest.
- `runs_registry.md` del especialista.
