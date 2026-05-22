# Biblioteca de bots / indicadores / herramientas

La carpeta `library/` agrupa los artefactos vivos del proyecto (bots de
backtest, indicadores, tools) en directorios autocontenidos. Cada entrada
conserva su lógica, sus parámetros default + presets nombrados, sus notas
narrativas (`notes.md`) y un manifest declarativo.

Esta guía documenta cómo navegar la biblioteca, cómo añadir entradas
nuevas y cómo se conecta con el motor de backtest y el `DataProvider`.

## Estructura del directorio

```
library/
  bots/<nombre>/
    manifest.yaml
    strategy.py            # opcional cuando entry_point referencia otro módulo
    notes.md
    presets/default.yaml
    presets/<otro>.yaml    # opcional, presets nombrados adicionales
    examples/              # invocaciones CLI / fixtures (opcional)
    tests/                 # tests específicos del bot (opcional)
  indicators/<nombre>/
    manifest.yaml
    indicator.py           # expone compute(candles, **params) -> None
    notes.md
  tools/<nombre>/
    manifest.yaml
    tool.py                # expone run(args, provider) o class Tool
    notes.md
  workspace/               # drafts en curso; NO auto-registrados
    .gitkeep
  _index.json              # autogenerado por `library refresh`
README.md                  # índice corto de la biblioteca
```

Todo en `library/` se versiona con git. Sólo `_index.json` se regenera a
mano cuando agregues o muevas una entrada.

## Esquema del `manifest.yaml`

```yaml
schema_version: 1
name: dorothy
kind: bot                       # bot | indicator | tool
version: 1.0.0
author: ""
description: "DCA con escalera de profit, modo Dorothy/Pecunator."
entry_point: "library.bots.dorothy.strategy:DorothyHubStrategy"
registry_aliases: ["dorothy_hub"]  # nombres extra para STRATEGY_REGISTRY
tags: ["dca", "long-only", "pecunator"]
created_at: "2026-05-22T00:00:00Z"
updated_at: "2026-05-22T00:00:00Z"
default_params:
  profit_factor: 0.05
  margin_drop_factor: 0.03
  quote_order_qty_usdt: 8.0
  max_rungs: 5
search_space:                   # hints Optuna-style
  profit_factor: { type: "float", min: 0.005, max: 0.08 }
  margin_drop_factor: { type: "float", min: 0.001, max: 0.02 }
  max_rungs: { type: "int", min: 2, max: 10 }
data_requirements:
  symbols: ["*"]               # "*" = cualquier símbolo, o lista
  intervals: ["1m", "1h"]
  required_columns: ["open", "high", "low", "close", "volume"]
  derived_columns: ["pec_trend"]
data_contributions:
  derived_dataset: null         # tools/indicadores pueden escribir aquí
notes_file: "notes.md"
preset_dir: "presets"
```

Reglas importantes:

- `entry_point` admite dos formatos:
  - `module.path:ClassName` para entradas que reutilizan un módulo ya
    importable (`backtest.strategies:DorothyHubStrategy`).
  - `library.<kind>s.<name>.<file>:Symbol` para entradas con código local;
    el cargador resuelve el archivo dentro de la carpeta de la entrada
    via `importlib.util.spec_from_file_location` (no requiere instalar
    `library/` como paquete).
- `kind: indicator` reemplaza el `StrategyBase` por una función
  `compute(candles, **params) -> None` que muta las velas in place.
- `kind: tool` reemplaza el `StrategyBase` por `run(args, provider)` o una
  clase `Tool` con un método `run`.
- `reference_only: true` desactiva el auto-registro y permite preservar
  entradas documentales (live-bots originales).

## Crear un bot desde cero

```bash
# 1) Scaffold del draft en library/workspace/mi_bot/
python backtest_cli.py library new mi_bot --kind bot

# 2) Editar:
#    library/workspace/mi_bot/strategy.py    (lógica)
#    library/workspace/mi_bot/manifest.yaml  (parámetros, search space, tags)
#    library/workspace/mi_bot/notes.md       (tesis, decisiones, observaciones)
#    library/workspace/mi_bot/presets/*.yaml

# 3) Validar (importa, instancia con defaults y revisa columnas requeridas):
python backtest_cli.py library validate mi_bot --workspace

# 4) Publicar (mueve workspace/ -> library/bots/ y regenera _index.json):
python backtest_cli.py library publish mi_bot

# 5) Ejecutar como cualquier otra estrategia:
python backtest_cli.py run --strategy mi_bot --symbol BTCUSDT --interval 1h
```

El paso 4 falla si la validación reporta errores; corrígelos antes de
publicar.

## Añadir un indicador o tool

- Para un indicador: `library new mi_rsi --kind indicator`. La función
  generada `compute(candles, **params)` debe anotar cada candle in place
  (mismo patrón que `backtest/pecunator_trend.py::annotate_pecunator_gates`).
- Para un tool: `library new mi_tool --kind tool`. La función `run(args,
  provider)` recibe el `argparse.Namespace` de la invocación y un
  `DataProvider` listo para leer/contribuir datos. Si prefieres clase,
  expón `class Tool` con un método `run(self, args, provider)`.

## DataProvider: bots, indicadores y tools sin acoplar al storage

`backtest/data_provider.py` define una interfaz limpia para que cualquier
entrada de la biblioteca lea klines (y, opcionalmente, contribuya datasets
derivados) sin saber si la fuente es Parquet, SQLite o Postgres mañana.

```python
from backtest.data_provider import get_data_provider

provider = get_data_provider()           # parquet si data/klines/_manifest.json existe
candles = provider.load_candles(
    symbol="XRPUSDT", interval="1h",
    start_ts=1735689600000, end_ts=1738367999000,
)
for candle in provider.iter_candles("XRPUSDT", "1m"):
    ...
provider.contribute_derived(
    "mi_rsi",
    rows=[{"open_time": 0, "value": 50.0}, ...],
    schema={"open_time": "int64", "value": "float64"},
)
```

Selección de backend por orden de precedencia:

1. Argumento explícito a `get_data_provider(backend="sqlite")`.
2. Variable de entorno `BACKTEST_DATA_BACKEND` (`parquet` o `sqlite`).
3. Auto: `parquet` si existe `data/klines/_manifest.json`, si no `sqlite`.

Backends incluidos:

- `ParquetDataProvider`: lee `data/klines/symbol=*/interval=*/year=*/month=*/part-000.parquet`.
  Si el rango pedido no está completamente materializado, cae a
  `db.iter_query_klines` de manera transparente.
- `SQLiteDataProvider`: usa `db.query_klines` / `db.iter_query_klines` para
  setups que aún no corrieron el backup Parquet.

## Catálogo inicial

### Bots migrados (`library/bots/`)

| Nombre | Clase backend | Tags |
| --- | --- | --- |
| `sma_cross` | `SmaCrossStrategy` | `trend`, `long-only`, `baseline` |
| `dorothy` (alias `dorothy_hub`) | `DorothyHubStrategy` | `dca`, `pecunator`, `trend-gated` |
| `dorothy_legacy` | `DorothyBacktestStrategy` | `dca`, `legacy` |
| `elphaba` (alias `elphaba_hub`) | `ElphabaHubStrategy` | `dca-inverse`, `pecunator` |
| `ha_trend` | `HeikinAshiTrendStrategy` | `heikin-ashi`, `trend` |
| `masha` | `MashaStrategy` | `trend-follow`, `pullback` |
| `thusnelda` | `ThusneldaPlaceholderStrategy` | `placeholder`, `wip` |
| `louise` | `LouiseStrategy` | `dca`, `tp-promedio` |
| `louise_lucky` | `LouiseLuckyStrategy` | `dca`, `lucky-strike` |
| `anti_louise` | `AntiLouiseStrategy` | `dca-inverse`, `spot` |
| `anti_louise_lucky` | `AntiLouiseLuckyStrategy` | `dca-inverse`, `lucky-strike` |

### Referencias live (`reference_only: true`)

| Nombre | Fuente |
| --- | --- |
| `dorothy_live_reference` | `aportes/dorothy.py` |
| `masha_live_reference` | `aportes/masha.py` |
| `thusnelda_live_reference` | `aportes/thusnelda.py` |

Estas tres entradas NO se registran en `STRATEGY_REGISTRY`; sólo existen
para preservar la lógica del bot live original como contexto al lado del
adaptador de backtest correspondiente.

## Comandos del CLI

```bash
python backtest_cli.py library list [--kind bot|indicator|tool] [--tag <tag>] [--workspace]
python backtest_cli.py library show <name>
python backtest_cli.py library new <name> [--kind bot|indicator|tool]
python backtest_cli.py library publish <name> [--kind bot|indicator|tool]
python backtest_cli.py library validate <name> [--workspace]
python backtest_cli.py library notes <name>
python backtest_cli.py library presets <name>
python backtest_cli.py library refresh
python backtest_cli.py library import-aporte <name>
```

Tras inicializar la base de datos (`init_db`), el CLI llama de manera
idempotente a `backtest.library.register_with_strategy_registry()`, así
que las entradas del directorio `library/bots/` quedan inmediatamente
disponibles para `python backtest_cli.py run --strategy <library_bot>`.

## Roadmap

- Fase 0 (próxima): integrar Postgres como tercer backend del
  `DataProvider`. El contrato actual se mantiene; sólo añadiremos
  `PostgresDataProvider` y un nuevo valor para
  `BACKTEST_DATA_BACKEND=postgres`.
- Más adelante: portar la lógica de `aportes/*.py` a adaptadores
  `library/bots/` reales que reemplacen los placeholders y referencias.
