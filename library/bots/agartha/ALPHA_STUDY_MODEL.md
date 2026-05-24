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

## Espacio de busqueda Optuna

### Saltos grandes (80 trials)
- `trailing_stop_pct`: `{10, 15, 20, 25, 28, 30, 35, 40, 50, 60}`
- `activation_profit_pct`: `{0, 10, 25, 50, 75}`
- `breakeven_lock_pct`: `{0, 10, 25, 50}`

### Ridiculos / extremos (20 trials forzados via `enqueue_trial`)
- `trailing_stop_pct`: `{1, 3, 5, 75, 80, 90}`
- `activation_profit_pct`: `{0, 100, 150, 200}`
- `breakeven_lock_pct`: `{0, 75, 100}`

Objetivo: detectar **oportunidades ocultas** que TPE no exploraria por si solo,
y mapear el espectro completo de comportamiento del bot frente al simbolo.

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

## Estudios realizados

| Simbolo | Fecha | Best return | Best trailing | Notas |
|---|---|---:|---:|---|
| **BILL** | 2026-05-24 | **+482.93 %** | 28.5 % | Pump claro 4-16 mayo; sweet spot estrecho |
| **PHAROS** | 2026-05-24 | **-1.40 %** | 40.0 % | Sin pump; todos los seteos negativos. Mejor: defensivo |
| **PLAY** | 2026-05-24 | **+242.34 %** | 5.0 % | Pump abril; trailing **ultra-corto** (5%) + activation 200% gana; quote USDC (USDT vacio) |

(añadir filas conforme se evaluen nuevos simbolos)
