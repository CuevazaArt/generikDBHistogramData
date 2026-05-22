# Núcleo Rust (`crates/genericbt-core`)

## Qué es y por qué

`crates/genericbt-core` es el crate nativo en Rust que implementa el *hot
path* del motor de backtesting:

- el **loop bar-a-bar** (`engine.rs`),
- el **simulador de broker spot** (`broker.rs`, 1:1 con
  `backtest/broker.py`),
- los **indicadores** SMA, EMA, RSI y ATR (`indicators.rs`, 1:1 con
  `backtest/indicators.py`),
- y las **transformaciones** Heikin-Ashi y selección de `price_source`
  (`transforms.rs`, 1:1 con `backtest/transforms.py`).

Se expone a Python como una *extension module* construida con
[`pyo3`](https://pyo3.rs) + [`maturin`](https://www.maturin.rs/) y se
importa desde el paquete `genericbt_core` (shim Python en la raíz del
repo). La frontera Python/Rust está dibujada deliberadamente para no
romper el API actual de estrategias: el callback `StrategyBase.on_bar` se
sigue ejecutando en Python y Rust solo lo invoca por barra, soltando el
GIL en el resto del loop.

¿Por qué portar a Rust?

- **Rendimiento**: el motor Python actual itera por una `List[Dict]` en
  puro CPython con GIL. Mover indicadores, broker y la mecánica de loop
  a Rust evita ~3–5x del overhead por barra en datos 1s anuales.
- **RAM**: el crate trabaja con buffers `Vec<f64>` densos en lugar de
  triplicar la lista de candles para calcular SMAs adicionales, como
  hacía la versión Python original.
- **Sin GIL en el *hot path***: solo se adquiere el GIL para construir
  `StrategyContext` y llamar a `on_bar`. Todo el resto (broker, indica-
  dores, mark-to-market, contabilidad de PnL) corre en Rust sin tocar
  objetos Python, lo que abre la puerta a paralelizar trials reales en
  Fase 3.
- **Paridad numérica garantizada**: misma semántica de redondeo, mismos
  *tolerance gates* (clamp `[0,1]` por `max(0.0, min(1.0, x))`, zero-out
  de `position_qty` a `1e-12`, etc.). El harness en
  `tests/test_engine_rs_parity.py` audita esto a 12 dígitos.

## Compilación local

Requisitos:

- **Rust 1.81+** (cualquier toolchain reciente sirve; se prueba contra
  estable). Instalar vía [`rustup`](https://rustup.rs).
- **Python 3.11+** (la wheel usa `abi3-py311`, así que el mismo binario
  sirve para 3.11, 3.12 y 3.13 sin recompilar).
- **maturin**: `pip install -r requirements-dev.txt` (o
  `pip install "maturin>=1.7,<2.0"`).

Build y registro de la extensión en el venv activo:

```bash
maturin develop --manifest-path crates/genericbt-core/Cargo.toml --release
```

Tras esto, `python -c "import genericbt_core; print(genericbt_core.is_rust_available())"`
debe responder `True`. La extensión queda en
`genericbt_core/_genericbt_core.<abi3>.{so,pyd}` (junto al stub
`__init__.pyi` que ya está en el repo para los analizadores estáticos).

> Nota: `[build-system]` en `pyproject.toml` apunta a `maturin` como
> *build backend*. Por eso `pip install .` también compila la wheel
> automáticamente si hay toolchain Rust disponible.

## Distribución vía CI

El workflow `.github/workflows/wheels.yml` construye wheels prebuiltas
para:

- `linux x86_64 / aarch64` con `manylinux: auto`,
- `windows x64 / x86`,
- `macos x86_64 / aarch64`,

y un `sdist` adicional. Las wheels son **abi3-py311**, así que una sola
artefactual sirve para `python-versions: ['3.11', '3.12', '3.13']` aunque
el matrix dispare un job por versión (es barato; cada job tarda
~minutos). El workflow está gateado por
`hashFiles('crates/genericbt-core/Cargo.toml') != ''`, así que se
activa automáticamente ahora que el crate existe.

La publicación a PyPI quedó preparada pero comentada en el workflow:
hay que configurar *trusted publishing* (OIDC) y descomentar el bloque
`publish:` antes de etiquetar un release.

## Cuándo cae al *fallback* Python

El shim `genericbt_core/__init__.py` selecciona el motor con esta regla:

```
if _RUST_AVAILABLE and os.getenv("BACKTEST_ENGINE_KIND", "python").lower() == "rust":
    -> rust path
else:
    -> backtest.engine.run_backtest (pure Python)
```

Es decir, el motor Python se usa cuando:

1. la wheel `_genericbt_core` no está instalada (caso típico en laptops
   de desarrollador sin toolchain Rust), **o**
2. `BACKTEST_ENGINE_KIND` no está seteada, o vale `"python"` u otra
   cosa que no sea `"rust"`.

El CLI hermano (`backtest_cli.py --engine ...`) escribe esa variable de
entorno; los tests y scripts pueden hacerlo manualmente (ver
`tests/test_engine_rs_parity.py`).

## Frontera Python / Rust

La frontera está pensada para no romper el API de estrategias:

| Componente              | Lenguaje | Notas                                        |
| ----------------------- | -------- | -------------------------------------------- |
| `StrategyBase.on_bar`   | Python   | Sigue siendo el contrato público.            |
| `StrategyContext`       | Python   | Construido por Rust por cada barra (cheap).  |
| `SpotBroker`            | Rust     | 1:1 con `backtest/broker.py`.                |
| Indicadores SMA/EMA/RSI/ATR | Rust | 1:1 con `backtest/indicators.py`.           |
| Heikin-Ashi             | Rust     | 1:1 con `backtest/transforms.py`.            |
| Loop por barra          | Rust     | `loop_seconds`, `events_mode`, snapshots.    |
| Pre-pass de candles     | Python (por ahora) | Fase 2 lo moverá a Arrow + Rust.   |
| Persistencia (Parquet)  | Python (por ahora) | Fase 2 introduce checkpoint writer. |

Las estrategias actuales en `backtest/strategies.py` corren sin
modificación. Cuando se reescriba una estrategia como `RustStrategy`
nativa (futuro), el callback Python por barra desaparece y se desbloquea
el rendimiento máximo.

## Cómo verificar paridad numérica

El harness oficial vive en `tests/test_engine_rs_parity.py`:

```bash
# Verificación full (incluyendo el path Rust si la wheel está instalada).
python -m pytest tests/test_engine_rs_parity.py -v

# Forzando el path Python (siempre disponible):
BACKTEST_ENGINE_KIND=python python -m pytest tests/test_engine_rs_parity.py -v

# Forzando el path Rust (requiere maturin develop previo):
BACKTEST_ENGINE_KIND=rust python -m pytest tests/test_engine_rs_parity.py -v
```

El harness genera 5000 barras de un seno sobre una tendencia (con `numpy`
si está disponible, si no con `random.Random(42)` para mantener
determinismo). Luego corre `SmaCrossStrategy(fast=10, slow=30)` con la
ingeniería Python de referencia y la compara — clave por clave — contra
la salida del shim. La aserción usa `math.isclose(rel_tol=1e-12,
abs_tol=1e-9)` sobre las 11 métricas canónicas devueltas por
`backtest.metrics.summarize_metrics`:

```
initial_cash, final_equity, total_return, max_drawdown, sharpe,
sortino, calmar, ulcer_index, win_rate, profit_factor, num_trades
```

Si alguna métrica divergiera más allá de la tolerancia, el assert
imprime el delta exacto por clave, lo que facilita aislar el bug del
lado Rust (broker, indicador o loop).

## Pruebas adicionales

`tests/test_genericbt_core_shim.py` cubre el contrato del shim:

- `import genericbt_core` debe funcionar siempre.
- `is_rust_available()` devuelve `bool`.
- El fallback Python se usa si la wheel falta, incluso con
  `BACKTEST_ENGINE_KIND=rust`.
- Cuando la wheel está disponible, el path Rust devuelve la misma
  estructura de `BacktestResult` (paridad numérica en el harness aparte).

## Checkpointing y resume

Fase 2 añadió `crates/genericbt-core/src/checkpoint.rs` (`CheckpointRs`
+ `write_checkpoint` / `read_checkpoint`) y enganchó la lógica al
`run_loop` en `engine.rs`. Los archivos JSON son intercambiables con
los del engine Python; el schema, los disparadores (cada N barras o
cada N segundos de sim) y la mecánica de resume (slicing del iterador
de velas + restauración del broker y `strategy.import_state(...)`) se
documentan en detalle en [`CHECKPOINTING.md`](CHECKPOINTING.md).
