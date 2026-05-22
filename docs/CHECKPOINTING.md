# Checkpointing y resume (Fase 2)

Esta nota cubre el subsistema de **checkpoints** y **resume** del motor de
backtesting introducido en la Fase 2 del rediseño. Los archivos relevantes:

- `backtest/checkpoint.py` — dataclass `Checkpoint` + helpers JSON
  (`write_checkpoint`, `read_checkpoint`, `latest_checkpoint_path`).
- `backtest/engine.py` — campos nuevos en `EngineConfig` y triggers de
  escritura/restauración dentro de `run_backtest()`.
- `backtest/runner.py` — `execute_and_persist_resumable()` (resuelve el
  último checkpoint para un `run_id` y lo pasa al engine).
- `crates/genericbt-core/src/checkpoint.rs` — equivalente Rust (mismo
  schema JSON).
- `backtest_cli.py` — flags `--checkpoint_every_bars`,
  `--checkpoint_every_sim_seconds`, `--checkpoints_dir` y `--resume`.

## ¿Qué dispara un checkpoint?

Dentro del loop de `run_backtest`, después de aplicar el clamp de
`loop_seconds` y antes de generar la señal del bar, se evalúan dos
umbrales independientes:

- `checkpoint_every_bars` — número de barras consecutivas *procesadas*
  desde el último checkpoint. `None` lo desactiva.
- `checkpoint_every_sim_seconds` — diferencia, en segundos de tiempo
  simulado (`open_time` del candle), entre el último checkpoint y el bar
  actual. Si todavía no se escribió ninguno, la primera barra observada
  ancla el reloj sin emitir un checkpoint en el offset `-1`.

Si **cualquiera** de los dos umbrales se cumple, se escribe un archivo
JSON en `checkpoints_dir` y se reinician los contadores.

Cuando `checkpoint_every_bars`, `checkpoint_every_sim_seconds` *y*
`resume_from_checkpoint` son `None`, el código no entra al bloque
nuevo: la ruta rápida queda **byte-idéntica** al motor pre-Fase-2. El
test `tests/test_checkpoint.py::test_engine_no_regression_when_disabled`
garantiza esto comparando `metrics`, `events` y `equity_curve` de
ambas variantes con igualdad estricta (no `pytest.approx`).

## ¿Dónde caen los archivos?

El layout sigue `backtest.storage_paths.StoragePaths`:

```
data/checkpoints/run_<id>/cp_<sim_ts>.json
```

donde `<sim_ts>` es el `open_time` (ms UTC) del candle que estaba a
punto de procesarse cuando se escribió el snapshot. La escritura es
**atómica**: se crea un sidecar `cp_<sim_ts>.json.<rand>.tmp` y luego se
hace `os.replace` sobre el target. Si el escritor crashea a mitad, el
target original queda intacto.

El payload JSON respeta el schema que persiste `meta.checkpoints` en
PostgreSQL (Fase 0):

```json
{
  "run_id": 42,
  "sim_ts": 1700000000000,
  "candle_offset": 99,
  "broker_state": {"cash": 9999.5, "position_qty": 0.01, "avg_entry": 50000.0},
  "strategy_state": {"...": "...opaco por estrategia..."},
  "seq": 17,
  "last_exec_ts": 1700000000000,
  "last_snapshot_ts": null,
  "last_trade_entry": [50000.0, 0.005],
  "created_at": "2026-05-22T15:30:00+00:00",
  "engine_kind": "python",
  "engine_version": "0.2.0"
}
```

## Resume end-to-end

Hay dos caminos:

### CLI: `--resume <run_id>`

```bash
python backtest_cli.py run \
    --strategy dorothy --symbol BTCUSDT --interval 1h \
    --start_ts 1704067200000 --end_ts 1735689600000 \
    --checkpoint_every_bars 1000 \
    --resume 123
```

El flujo es:

1. `_apply_runtime_flags` setea `BACKTEST_RESUME_RUN_ID=123`.
2. `_run_once` detecta el env var y despacha a
   `execute_and_persist_resumable` en lugar de `execute_and_persist`.
3. `execute_and_persist_resumable`:
   1. Resuelve `checkpoints_dir = data/checkpoints/run_123` (honrando
      `BACKTEST_DATA_ROOT`).
   2. Llama a `latest_checkpoint_path()` para el archivo con `sim_ts`
      más alto.
   3. Si lo encuentra, patchea `cfg.resume_from_checkpoint` y llama a
      `execute_and_persist`. Imprime `[resume] run_id=123 from <path>`.
   4. Si no hay checkpoint, imprime un aviso y arranca *from scratch*.
4. `run_backtest()` carga el JSON, restaura `broker.state`, llama
   `strategy.import_state(...)`, repone `seq` / `last_exec_ts` /
   `last_snapshot_ts` / `last_trade_entry`, y reanuda el loop en el
   índice `cp.candle_offset + 1`.
5. Se emite un evento sintético `event_type='resume'` con
   `payload={"checkpoint_path", "candle_offset", "engine_kind",
   "engine_version"}` para que la auditoría refleje el punto de
   reanudación.

### Programático

```python
from backtest.engine import EngineConfig, run_backtest

cfg = EngineConfig(
    db_path="klines.db",
    symbol="BTCUSDT",
    interval="1h",
    checkpoint_every_bars=1000,
    checkpoints_dir="data/checkpoints/run_123",
    resume_from_checkpoint="data/checkpoints/run_123/cp_1735000000000.json",
)
result = run_backtest(cfg, strategy_cls=MyStrategy)
```

## Auditoría

Cuando el backend metadata activo es PostgreSQL,
`execute_and_persist_resumable` inserta una fila *best-effort* en
`ops.audit_log`:

```sql
INSERT INTO ops.audit_log (run_id, event_type, payload)
VALUES (123, 'resume', '{"checkpoint_path": "data/checkpoints/run_123/cp_1735000000000.json"}'::jsonb);
```

La inserción está envuelta en `try/except`: si PG no está disponible o
psycopg no está instalado, el resume sigue adelante sin escribir la
fila — el dato es informativo, no parte del estado del run. Con backend
SQLite (legacy) la inserción se omite silenciosamente.

## Reglas de paridad

- El engine Python y el engine Rust producen archivos JSON
  intercambiables. La estructura del payload está documentada en
  `Checkpoint.to_dict()` (Python) y `CheckpointRs` (Rust); ambos lados
  usan los mismos nombres de campo.
- El campo `engine_kind` se conserva en el snapshot para permitir, a
  futuro, rechazar checkpoints cross-engine si la semántica del loop
  divergiera entre ambos.
- `candle_offset` siempre apunta al *último bar persistido* (no al
  próximo): la lógica de resume hace `skip while i <= candle_offset`,
  de modo que la barra exactamente en `candle_offset + 1` es la
  primera que vuelve a ejecutar `strategy.on_bar`.
