# Modelo canonico de estudio Alpha (Agartha)

Pipeline estandar para evaluar cualquier simbolo del mercado Binance Alpha
con el bot Agartha. **Toda nueva instancia de estudio sigue este modelo**
para que los entregables sean comparables entre simbolos.

Script orquestador: [`scripts/agartha_alpha_study.py`](../../../scripts/agartha_alpha_study.py)

---

## Estructura del estudio

Cada simbolo Alpha produce los siguientes artefactos bajo `reports/entregables/`:

```
entregables/
├── datasets/<symbol>_<interval>_alpha/
│   ├── manifest.json           (window, integrity, parquet paths, git)
│   ├── alpha_token.json        (metadata Alpha completa: alphaId, chain, liquidity, holders, listing)
│   ├── MANIFEST.md
│   └── window.jsonl  (si no hay parquet cache)
├── studies/agartha_<symbol>_<interval>_alpha_study/
│   ├── ALPHA_STUDY_INDEX.md    (indice consolidado: dataset + study + runs)
│   ├── spectrum.png            (overlay equity+DD de los 100 trials)
│   ├── trial_to_run.json       (mapping trial_no -> run_id + best params)
│   └── optuna.db               (SQLite Optuna aislado del db principal)
└── strict/
    ├── agartha_<symbol>_<interval>_pilot_<ts>/  (Run A: cash=10 multitrades)
    │   ├── equity_drawdown.png
    │   ├── RUN_SUMMARY.md
    │   └── run_manifest.json
    ├── agartha_<symbol>_<interval>_pilot_<ts>/  (Run B: cash=100 single-shot)
    └── agartha_<symbol>_<interval>_pilot_<ts>/  (Run C: cash=100 multitrades)
```

---

## Pasos del pipeline

| # | Paso | Script | Tiempo tipico |
|---|---|---|---:|
| 1 | Resolve symbol (token list + exchange-info, auto-quote USDT/USDC) | inline | < 2 s |
| 2 | Download alpha klines (full history desde listing) | `download_and_prepare_alpha.py` | 5-30 s |
| 3 | Cura timestamps + escribe parquet cache | (mismo) | < 2 s |
| 4 | Optuna spectrum 100 trials (80 normales + 20 extremos forzados) | `agartha_optuna_spectrum.py` | 10-15 s |
| 5 | Genera spectrum.png + trial_to_run.json | (mismo) | < 2 s |
| 6a | Run A: `initial_cash=10`, `max_cycles=0` (multitrades) | `run_agartha_bill_pilot.py` | < 2 s |
| 6b | Run B: `initial_cash=100`, `max_cycles=1` (single-shot) | (mismo) | < 2 s |
| 6c | Run C: `initial_cash=100`, `max_cycles=0` (multitrades) | (mismo) | < 2 s |
| 7 | Escribe `ALPHA_STUDY_INDEX.md` consolidado | inline | < 1 s |

**Total: ~20-40 segundos por simbolo** (depende de la cantidad de velas descargadas).

---

## Espacio de busqueda Optuna (granularidad fina)

### Saltos finos (80 trials)
- `trailing_stop_pct`: `{8, 10, 12, 15, 18, 20, 22, 25, 28, 30, 33, 35, 38, 40, 45, 50, 55, 60}` (18 valores)
- `activation_profit_pct`: `{0, 5, 10, 20, 30, 40, 50, 65, 80}` (9 valores)
- `breakeven_lock_pct`: `{0, 5, 10, 20, 30, 40, 60}` (7 valores)
- Total combinaciones: **1 134** (TPE explora 80 con sampling inteligente).

### Ridiculos / extremos (20 trials forzados via `enqueue_trial`)
- `trailing_stop_pct`: `{0.5, 1, 2, 3, 5, 70, 75, 80, 85, 90, 95}`
- `activation_profit_pct`: `{0, 90, 100, 120, 150, 180, 200, 250}`
- `breakeven_lock_pct`: `{0, 70, 75, 85, 100, 125, 150}`

Objetivo: detectar **oportunidades ocultas** que TPE no exploraria por si solo,
y mapear el espectro completo de comportamiento del bot frente al simbolo.
Validado empiricamente: BSB (+662%) y UP (+330%) salieron del cluster extremo
(trailing 80% + breakeven 75%), no del cluster normal.

---

## Spectrum plot (`spectrum.png`)

Grafica unificada de los 100 trials:

- **Panel superior**: equity de cada trial overlay con `alpha=0.35`.
- **Panel inferior**: drawdown de cada trial overlay con `alpha=0.30`.
- **Color**: `RdYlGn` codificado por `total_return` final (rojo = peor, verde = mejor).
- **BEST y WORST** destacados con linewidth doble y labels en la leyenda.
- **Colorbar**: escala de `total_return%` para interpretacion directa.

Permite ver en un solo vistazo:
- Si el simbolo tuvo un pump (banda verde concentrada arriba) o no (banda roja).
- Dispersion de resultados (banda ancha = sensible a params; estrecha = robusto).
- Zona de DD comun a todos los seteos (riesgo intrinseco del simbolo).

---

## Runs canonicos (best params del study)

| Run | initial_cash | max_cycles | Pregunta que responde |
|---|---:|---:|---|
| **A** | **10** | **0** | "¿Que pasa con 1 instancia minima de capital de riesgo?" |
| **B** | **100** | **1** | "¿Cuanto captura un single-shot puro?" (referencia HODL-like) |
| **C** | **100** | **0** | "¿El ciclo continuo añade o destruye valor en este simbolo?" |

Comparar A vs B en **retorno absoluto USDT** (deberian ser similares en simbolos que pumpean).
Comparar B vs C para decidir si el multi-cycle aporta alfa en este simbolo concreto.

---

## Uso

```powershell
# Estudio completo (default 15m):
python scripts/agartha_alpha_study.py --symbol PHAROS

# Otro intervalo:
python scripts/agartha_alpha_study.py --symbol BILL --interval 5m

# Re-correr sin re-descargar:
python scripts/agartha_alpha_study.py --symbol PHAROS --skip_download

# Solo runs canonicos (reutilizar study existente):
python scripts/agartha_alpha_study.py --symbol PHAROS --skip_download --skip_optuna

# Custom search budget:
python scripts/agartha_alpha_study.py --symbol BILL --trials 200 --extreme 40
```

---

## Reglas arbitrarias detectadas

Documentar aqui cada regla no documentada que aparezca en el camino. Estas
son **adaptaciones de produccion** que ya estan en el codigo.

| Regla | Simbolo donde se detecto | Mitigacion |
|---|---|---|
| **Quote asset variable (USDT vs USDC)** | PHAROS solo USDC en Alpha; BILL solo USDT | `resolve_alpha_symbol` cruza con `get-exchange-info` y elige el quote tradeable real |
| **API code `-1121 Invalid symbol`** para tokens muy nuevos | PHAROS antes del fix | Añadido a `ALPHA_FATAL_CODES`; ya no retry, raise inmediato visible |
| **End-of-stream `-1000 No records found`** | BILL al final del histórico | Tratado como termino normal de paginacion |
| **Mismo human symbol con multiples alphaIds** (cross-chain duplicates) | PLAY: `ALPHA_822` (Base, activo, liq 831k) y `ALPHA_300` (BSC, offline+offsell) | Scoring `(not offline, not offsell, liquidity, volume24h)`; warning con alternativas descartadas |
| **Par USDT registrado pero sin velas** (exchange-info incluye el par, klines devuelve 0) | PLAY: `ALPHA_822USDT` listado pero vacio, solo `ALPHA_822USDC` tiene datos | **Sample probe** con `limit=5` por candidato; descartar si vacio y probar el siguiente quote |
| **Token registrado en token list pero sin par tradeable** (catalogo inactivo) | LRCXon (`ALPHA_899`, BSC, `liquidity=None`, `volume24h=0`, `tradeable=[]`) | Detectado con `alpha_symbols_for_alpha_id`; pipeline falla con mensaje claro `"AlphaId X sin pares tradeables en exchange-info"`. **No descargar.** |

Cuando aparezca una nueva regla:
1. Logguearla en este archivo (tabla anterior).
2. Adaptar `binance_hist_downloader.py` o `agartha_*` segun corresponda.
3. Añadir test en `tests/test_alpha_resolver.py`.
4. Anotar el fix en el commit message como `(adapt: <symbol>)`.

---

## Rutina obligatoria del tester (resolucion de simbolo Alpha)

**Antes de descargar klines o lanzar runs**, todo simbolo Alpha pasa por
estas validaciones (implementadas en `BinanceDownloader.resolve_alpha_symbol`
y replicadas en `download_and_prepare_alpha.py` + `agartha_alpha_study.py`):

1. **Token list lookup**: encontrar el simbolo humano en `/bapi/.../token/list`.
2. **De-duplicacion por alphaId**: si el mismo symbol aparece N veces (distintos
   chains), elegir por scoring `(not offline, not offsell, liquidity, volume24h)`
   descendente. Emitir warning listando alternativas descartadas.
3. **Cross-check con exchange-info**: validar que existe par tradeable y detectar
   el **quote correcto** (USDT vs USDC). Si el quote pedido no esta, fallback
   al primer pair disponible con warning.
4. **Persistir** el simbolo humano normalizado (`PHAROS` -> `PHAROSUSDC`) en
   `klines.db` y en `alpha_token.json` para trazabilidad.

Cobertura de tests: `tests/test_alpha_resolver.py` (regresion de cada regla).

---

## Accesorio: Entry-LIMIT pre-colocada (recomendado para Alpha)

Encaje natural con la naturaleza de Alpha: **dejas que el "cuchillo cayendo"
caiga** y un orden LIMIT en el orderbook te llena al precio favorable sin
necesitar monitoreo activo. Server-side, cero compute, escala a N instancias.

### Parametros (en `AgarthaStrategy`)

| Parametro | Default | Rol |
|---|---:|---|
| `entry_limit_offset_pct` | 0.0 | % debajo del precio activo. **0 = comportamiento legacy** (compra inmediata). >0 = coloca LIMIT a `precio * (1 - X/100)`. |
| `entry_limit_expiry_bars` | 0 | Barras antes de cancelar/re-evaluar la LIMIT pendiente. 0 = GTC sin expiracion. |
| `entry_limit_reprice_on_expiry` | False | Tras expirar: re-cotizar al nuevo precio (True) o cancelar y esperar el proximo ciclo (False). |

### Mecanica backtest

- Primer bar sin posicion -> "coloca" la LIMIT en `pending_limit_price`.
- Cada bar siguiente: si `low <= pending_limit_price`, simula fill al limit_price.
- Si pasan `entry_limit_expiry_bars` sin fill, cancela (o re-cotiza).

### Mecanica live

1. Al activar el bot: leer `@ticker` para precio actual.
2. Calcular `limit_price = current * (1 - offset/100)`.
3. Validar que `limit_price` este dentro de banda `PERCENT_PRICE_BY_SIDE`
   (ver `agartha_exit_planner.compute_sell_band` extendido a buys).
4. POST LIMIT GTC.
5. Esperar fill via `userDataStream`; cuando entra, arranca la logica trailing.
6. Si no fillea en N horas, cancelar y decidir (reprice / abandonar).

### Por que es superior al Entry Gate WS-driven en Alpha

| Dimension | Entry Gate WS (3 capas) | **LIMIT pre-colocada** |
|---|---|---|
| Filosofia | Detecto el momento y compro market | **Declaro precio favorable, el mercado viene a mi** |
| Carga compute | Tick monitor per simbolo | **0 (orden vive en el exchange)** |
| Escala a 50 instancias | 50 WS + 50 deques + 50 evals/tick | **50 LIMITs estaticas en el book** |
| Cuchillos cayendo | Bloquea (Layer 3 momentum) | **Te llenan al precio que tu elegiste** |
| Coherente con LIMIT-only de Alpha | No (emula market con LIMIT agresiva) | **Si (LIMIT es el unico tipo soportado)** |
| Risk si bot cae | Pierdes oportunidad | **LIMIT sigue activa hasta cancelacion** |

**Recomendacion**: para deployment Alpha, **usar `entry_limit_offset_pct > 0`**
y mantener el Entry Gate WS solo como herramienta de analisis/screening.

Cobertura: `tests/test_agartha.py` (6 casos del accesorio LIMIT) + el Entry
Gate WS se conserva en `backtest/agartha_entry_filter.py` para usos no
operacionales (research, dashboards).

---

## Accesorio (alternativo): Entry Gate WS (`backtest/agartha_entry_filter.py`)

Modulo opcional para identificar el **momento favorable de armado** del bot
(en lugar de comprar "al activar"). Diseñado para uso live (WS-driven via
`AgarthaWsMonitor`) y testeable en backtest.

### 3 capas (configurable por activo)

1. **Donchian low(N)** — trigger: precio actual <= `min(low, N velas)` del
   timeframe operativo (default N=20 en 15m = 5h de contexto).
2. **Macro MA filter** — bloquea si precio < `MA(M, source) * (1 - drop_pct)`
   en timeframe superior (default M=20 en 4h, drop_pct=30%). Evita cuchillo cayendo.
3. **Momentum uptick** — exige que la ultima vela cerrara arriba de la anterior
   (o que la HA-candle sea verde). Confirma reversion iniciada.

`ARMED = Layer1 AND Layer2 AND Layer3`. Cualquier capa puede desactivarse
seteando su lookback a 0 (Layer 3 con `require_momentum_uptick=False`).

### Por que es mejor que `MM(3, low) 4h` sola

- `MM(3, low)` es solo una version reactiva de Layer 2 con N pequeño.
- En crash continuo la MA cae con el precio -> compra cada vela.
- No tiene confirmacion de reversion (Layer 3) -> compra mid-crash.
- No tiene Donchian (Layer 1) -> compra en wobbles intrabar sin contexto.

### Uso live (stub `AgarthaWsMonitor`)

```python
from backtest.agartha_entry_filter import AgarthaWsMonitor, EntryGateConfig

cfg = EntryGateConfig(
    donchian_lookback=20, donchian_tolerance_pct=0.5,
    macro_ma_lookback=20, macro_ma_source="close", macro_drop_pct=30.0,
    require_momentum_uptick=True,
)
mon = AgarthaWsMonitor(cfg)

# WS-driven feed:
ws_kline_15m.on_message(lambda c: mon.push_operating_candle(c))
ws_kline_4h.on_message(lambda c: mon.push_macro_candle(c))
ws_trade.on_message(lambda t: handle(mon.on_tick(t['price'])))

def handle(decision):
    if decision.armed:
        agartha.start()  # lanza el bot solo cuando ARMED
```

Cobertura: `tests/test_agartha_entry_filter.py` (8 casos, todos verdes).

---

## Re-optimizacion completa con LIMIT (22 symbols, 2026-05-25)

Los 22 symbols evaluados pasaron por re-optimizacion con `entry_limit_offset_pct`
incluido en el espacio Optuna. **Todos mejoraron o quedaron iguales**; los
sin-pump pasaron de negativos a positivos al permitir LIMIT profundas.

### Mejoras destacadas vs primera optimizacion

| Simbolo | Antes | Despues | Mejora |
|---|---:|---:|---:|
| **BSB** | +662 % | **+918.64 %** | **+256 pp** |
| **CHECK** | −0.47 % | **+223.68 %** | **+224 pp** (de perdedor a winner) |
| **SHARE** | −1.41 % | +0.00 % | +1.4 pp |
| **PHAROS** | −1.40 % | **+13.05 %** | +14 pp |
| **B2** | −2.22 % | **+7.16 %** | +9 pp |
| **ARTX** | −0.54 % | **+141.51 %** | **+142 pp** (LIMIT 80%!) |
| **WMTX** | +34 % | +81 % | +47 pp |

**Total: 22/22 symbols ahora positivos o = 0** (LRCXon sigue siendo no-tradeable).

---

## Validacion bimodal con n=75 symbols (2026-05-25)

Batch de 55 symbols nuevos lanzado con el espacio Optuna **restringido al
bimodal** (`NORMAL_BREAKEVEN = [0, 40, 50, 60]`, `EXTREME_BREAKEVEN = [0, 70, 85, 100]`).
Resultado: **55/55 OK** en ~42 minutos.

### Confirmacion del patron (promedio global por bin, n=75)

| Bin `breakeven_lock_pct` | Avg return (n=75) |
|---|---:|
| **be=0** | **+97.9 %** |
| 1-15 | +63.1 % (data legacy, no explorado) |
| 15-35 | +33.3 % (legacy, valle confirmado) |
| **35-65** | **+89.4 %** (sweet spot nuevo espacio) |
| 65-100 | +45.3 % |
| 100-200 | +44.6 % (legacy, lock casi inactivo) |

Distribucion del best `breakeven_lock_pct` (n=75):
- **be=0: 31 symbols (41 %)**
- 0<be<50: 5 symbols (legacy, en proceso de re-optimizacion)
- **be>=50: 39 symbols (52 %)**
- **93 % de los symbols caen en el bimodal** → confirmacion empirica.

### Nuevos winners descubiertos

| Symbol | Return | Best be | Best trailing | Best offset |
|---|---:|---:|---:|---:|
| **RAVE** | **+4043.81 %** | 50 | 28 % | 1 % |
| **LAB** | +2927.59 % | 85 | 95 % | 3 % |
| **BOB** | +1944.57 % | 0 | 40 % | 3 % |
| **RIVER** | +1264.34 % | 0 | 70 % | 35 % |
| **UB** | +691.46 % | 0 | 85 % | 35 % |
| **POWER** | +686.65 % | 0 | 70 % | 1 % |
| **GUA** | +670.00 % | 0 | 35 % | 1 % |
| **FOLKS** | +603.26 % | 0 | 38 % | 0 % |
| **PIEVERSE** | +584.40 % | 100 | 0.5 % | 35 % |

### Tasa de exito ampliada (n=75)

- **Winners (positivos)**: 67/75 = **89 %**
- Mega-winners (>500 %): 10 symbols
- Big winners (200-500 %): 21 symbols
- Moderados (50-200 %): 24 symbols
- Pequenos winners (0-50 %): 12 symbols
- Losers (<0 %): 8 symbols (todos entre 0 y −6.5 %)
- Break-even exactos: 3 (FIGHT, IP, SHARE, SKR)

### Cartera teorica (75 × 10 USDT = 750 USDT)

- Suma de retornos best-per-symbol: aprox **+85 000 % acumulado**
- Mejores 10 aportan el grueso (RAVE solo: ×40 sobre 10 USDT = +400 USDT)
- Pérdidas: ~−5 USDT cumulado (8 losers × ~−0.6 USDT promedio)
- **Cartera teorica equity final ~10x el capital invertido** (techo in-sample)

---

## Analisis: ¿el breakeven_lock aporta o estorba? (2026-05-25, primer analisis)

Cross-symbol study sobre **22 symbols × 100 trials cada uno = 2 200+ datapoints**.
Reporte completo: `reports/entregables/cross_studies/BREAKEVEN_ANALYSIS.md`.

### Patron observado (promedio por bin de breakeven)

| Bin `breakeven_lock_pct` | Avg return |
|---|---:|
| **be=0** (sin lock) | **+68.8 %** |
| 1-15 | +62.7 % |
| **15-35** | **+37.4 %** ← VALLE: el peor bin |
| 35-65 | **+65.2 %** ← sweet spot mega-pumps |
| 65-100 | +48.3 % |
| **100-200** | +54.5 % ← lock casi inactivo (equivale a be=0) |

### Veredicto

**El breakeven_lock_pct es BIMODAL en Alpha**:

- **be=0** funciona simple y competitivo (4/22 symbols).
- **be>=50** captura mega-pumps (13/22 symbols).
- **be intermedio (15-35) ESTORBA**: se activa muy temprano, vende antes del pump completo, y mata el upside asimetrico.

**Recomendacion operativa**: para deploy inicial usar **be=0** (simple, default).
Solo subir a be>=40 si Optuna lo confirma para ese symbol especifico. **Nunca**
usar be intermedio por defecto — empiricamente es el peor bin.

Diferente a estrategias clasicas (Louise/Dorothy) donde be moderado tiene sentido:
en Alpha, la asimetria de pumps (x5-x20) hace que cualquier salida prematura sea
catastrofica para la tesis. Mejor "no proteges nada y aceptas perderlo todo, o lo
proteges solo cuando ya doblaste/triplicaste".

---

## Mejora con LIMIT pre-colocada — re-optimizacion sobre 10 winners (2026-05-25)

Estudios re-corridos incluyendo `entry_limit_offset_pct` en el espacio Optuna
(`{0, 1, 2, 3, 5, 8, 12, 18, 25}` normales + `{0, 35, 50, 65, 80}` extremos).

| Simbolo | Anterior best | **Nuevo best (con LIMIT)** | Best offset | Mejora |
|---|---:|---:|---:|---:|
| **BSB** | +662 % | **+918.64 %** (trail 95, act 50, be 40) | **25 %** | **+256 pp** |
| **BTW** | +163 % | **+215.61 %** (trail 20, be 60) | **3 %** | +52 pp |
| **UP** | +330 % | **+368.11 %** (trail 75, act 65, be 70) | **8 %** | +38 pp |
| **JCT** | +185 % | **+219.20 %** (trail 12, act 250, be 5) | **8 %** | +33 pp |
| **STABLE** | +114 % | **+135.30 %** (trail 50, act 250) | **8 %** | +21 pp |
| **PLAY** | +242 % | **+260.88 %** (trail 8, act 250) | **2 %** | +19 pp |
| **EDGE** | +112 % | **+113.82 %** (trail 3, act 120, be 150) | **5 %** | +2 pp |
| **BILL** | +483 % | +482.81 % (trail 30, act 50, be 150) | 0 % | 0 (pump directo) |
| **TRIA** | +262 % | +262.24 % (trail 5, act 250, be 150) | 0 % | 0 (sweet spot ya en pico) |
| **ZEST** | +144 % | +144.43 % (trail 5, act 150, be 85) | 0 % | 0 |

**7 de 10 (70 %)** mejoraron con LIMIT >0 %. Offsets ganadores: mayormente **2-8 %**;
BSB destaca con **25 %** (combina con trailing 95 % extremo). Los tokens que
pumpean directo desde la entrada (BILL) o que ya tenian sweet spot saturado
(TRIA, ZEST) prefieren offset=0.

Cartera teorica (10 instancias x 10 USDT = 100 USDT):
- Sin LIMIT: +270 USDT (suma de mega-winners)
- **Con LIMIT optimizada: +312 USDT (+42 USDT = +15.5 % mejora)**

---

## Estudios realizados

| Simbolo | Fecha | Best return | Best trailing | Notas |
|---|---|---:|---:|---|
| **BILL** | 2026-05-24 | **+482.93 %** | 28.5 % | Pump claro 4-16 mayo; sweet spot estrecho |
| **PHAROS** | 2026-05-24 | **-1.40 %** | 40.0 % | Sin pump; todos los seteos negativos. Mejor: defensivo |
| **PLAY** | 2026-05-24 | **+242.34 %** | 5.0 % | Pump abril; trailing **ultra-corto** (5%) + activation 200% gana; quote USDC (USDT vacio) |
| **B2** | 2026-05-24 | **-2.22 %** | 1.0 % | BSquared Network (BSC); 20300 velas; sin pump; mejor caso es perder casi nada |
| **NEX** | 2026-05-24 | **+16.98 %** | 5.0 % | Nexus (BSC); solo 404 velas (4 dias); pump moderado |
| **LRCXon** | 2026-05-24 | **N/A** | - | **NO TRADEABLE**: registrado en token list pero `tradeable=[]`. Pipeline falla limpio. |
| **CHECK** | 2026-05-24 | **-0.47 %** | 25.0 % | Checkmate (Base, USDC); 5036 velas; sin pump |
| **BTW** | 2026-05-24 | **+163.45 %** | 20.0 % | Bitway (BSC); 8012 velas (84 dias); pump grande capturado |
| **IN** | 2026-05-24 | **+21.22 %** | 10.0 % | INFINIT (BSC, alphaId 312); 7781 velas (~81 d); pump moderado |
| **ZEST** | 2026-05-24 | **+144.43 %** | **3.0 %** | Zest Protocol (BSC, alphaId 970); 529 velas (~5 d); trailing ultra-corto + activation 150% |
| **BSB** | 2026-05-24 | **+662.74 %** | **80.0 %** | Block Street (BSC, alphaId 790); 7837 velas (~81 d); mega pump; trailing **extremo alto** + breakeven 75% |
| **UP** | 2026-05-24 | **+330.59 %** | **80.0 %** | Unitas (BSC, alphaId 804); 6969 velas (~72 d); pump grande; mismo perfil que BSB |
| **SHARE** | 2026-05-24 | **-1.41 %** | 30.0 % | ShareX Token (BSC, alphaId 956); 1597 velas (~16 d); sin pump significativo |
| **WMTX** | 2026-05-24 | **+34.22 %** | 10.0 % | WorldMobile (BSC); ~118 d; pump moderado |
| **TRIA** | 2026-05-24 | **+262.24 %** | **5.0 %** | TRIA (BSC); ~110 d; pump grande; trailing 5% + activation 250% + breakeven 150% (extremo) |
| **BASED** | 2026-05-24 | **+25.45 %** | 3.0 % | Based (BSC); ~55 d; pump moderado con perfil extremo |
| **STABLE** | 2026-05-24 | **+113.88 %** | 5.0 % | Stable (BSC); ~107 d; pump grande capturado por trailing corto |
| **ARTX** | 2026-05-24 | **-0.54 %** | 35.0 % | ARTX (BSC); ~117 d; sin pump |
| **CYS** | 2026-05-24 | **+17.13 %** | 3.0 % | CYS (BSC); ~65 d; pump pequeño con perfil extremo |
| **JCT** | 2026-05-24 | **+185.74 %** | **1.0 %** | JCT (BSC); ~109 d; pump grande; trailing ultra-corto + activation 250% + breakeven 75% |
| **AIA** | 2026-05-24 | **+15.07 %** | 3.0 % | AIA (BSC); ~76 d; pump pequeño |
| **EDGE** | 2026-05-24 | **+112.44 %** | **0.5 %** | EDGE (BSC); ~54 d; pump grande; trailing **MICRO** (0.5%) + activation 120% |
| **COAI** | 2026-05-24 | **+34.34 %** | 8.0 % | COAI (BSC); ~104 d; pump moderado |

(añadir filas conforme se evaluen nuevos simbolos)
