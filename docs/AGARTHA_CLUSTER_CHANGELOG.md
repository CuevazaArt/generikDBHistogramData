# Agartha Cluster - Bitácora de construcción (v0.1.0)

Snapshot del estado del cluster al cierre de la sesión del **25/05/2026**.
Resumen ejecutivo, decisiones tomadas, evidencia de validación, y la
única deuda técnica restante para puesta en producción.

Referencias:
- Diseño: [`AGARTHA_CLUSTER.md`](AGARTHA_CLUSTER.md)
- Guía paso a paso del operador: [`AGARTHA_CLUSTER_RUNBOOK.md`](AGARTHA_CLUSTER_RUNBOOK.md)

---

## 1. Alcance entregado

### Paquete `backtest/agartha_cluster/` (~3 562 LOC)

| Módulo | LOC | Responsabilidad |
|---|---:|---|
| `cluster_db.py` | 872 | DAO sobre SQLite WAL; migración idempotente |
| `bot_runner.py` | 603 | Ciclo de vida del bot (entry, trailing, exit, manual) |
| `cli.py` | 373 | Subcomandos: init-db, load-universe, set-params, schedule-batch, status, report, creds, live-up, supervisor |
| `live_client.py` | 373 | `LiveClient` Protocol + `StubLiveClient` operativo + `BinanceAlphaClient` skeleton |
| `models.py` | 233 | Enums + dataclasses puros |
| `credentials.py` | 227 | OS keyring (Windows/macOS/Linux) + fallback env-vars |
| `cluster_service.py` | 202 | Loop principal sincrónico cooperativo |
| `scheduler.py` | 137 | FIFO + slot 10 min + throttle-aware |
| `api_throttle.py` | 139 | Budgets rolling: weight/min + orders/10s |
| `event_logger.py` | 136 | Dual write: `event_log` (DB) + JSONL append-only |
| `state_machine.py` | 121 | Transiciones válidas con rechazo defensivo |
| `reconciler.py` | 112 | Snapshot periódico open orders + balance |
| `__init__.py` | 34 | API pública |

### Tests (`tests/test_agartha_cluster_*.py`, 571 LOC, **27 verdes en 2.58 s**)
- `test_agartha_cluster_db.py`: DAO, migraciones, queries críticas.
- `test_agartha_cluster_state_machine.py`: transiciones válidas/inválidas.
- `test_agartha_cluster_scheduler_throttle.py`: budgets + cadence.
- `test_agartha_cluster_runner_e2e.py`: 3 caminos completos con StubLiveClient.

### Documentación
- [`docs/AGARTHA_CLUSTER.md`](AGARTHA_CLUSTER.md): diseño canónico (12 secciones, 292 líneas).
- [`docs/AGARTHA_CLUSTER_RUNBOOK.md`](AGARTHA_CLUSTER_RUNBOOK.md): guía operativa paso a paso (7 fases + checklist).
- [`scripts/agartha_cluster_smoke.py`](../scripts/agartha_cluster_smoke.py): smoke E2E reutilizable (5 escenarios, exit 0 si todos pasan).

---

## 2. Decisiones de diseño relevantes

### 2.1 SQLite WAL como única fuente de verdad
- Un único archivo (`cluster.db`) hace backups triviales (`VACUUM INTO`).
- WAL permite a dashboards / CLI status leer sin bloquear escrituras del servicio.
- Trade-off aceptado: un solo proceso escritor. Suficiente para escala objetivo (1 deploy/10 min, ~400 símbolos).

### 2.2 Credenciales **nunca** en la DB
- Solo se persiste un puntero (`service_name`, `username`, `storage_method`) en `credentials_meta`.
- El secreto vive en el OS keyring; fallback documentado a variables de entorno.
- Trade-off aceptado: dependencia opcional de `keyring` (puro Python; multiplataforma).

### 2.3 State machine explícita (no estados implícitos)
- 17 estados, transiciones validadas por `state_machine.transition()`.
- Cada transición persiste fila en `bot_state_log` con `from_state`, `to_state`, `reason`, `correlation_id`.
- Permite reanudar el servicio sin pérdida: el runner lee la DB y continúa desde el estado correcto.

### 2.4 Trailing en el bot, no en Binance
- Binance Alpha solo acepta `LIMIT` (sin STOP_LOSS, OCO, trailingDelta). Decisión documentada en `library/bots/agartha/notes.md`.
- El bot mantiene `peak_price` + `trail_floor` y persiste cada actualización.
- Implicación: un crash sin persistencia del peak rompería el trailing → resuelto por persistencia inmediata cada tick.

### 2.5 Manejo escalonado de SELL LIMIT que no llena (regla principal del operador)
- 60 s sin fill → re-quote al bid actual (más agresivo).
- 5 min sin fill → re-quote al borde inferior de la banda permitida.
- 10 min sin fill → `stale_exit` + alerta `needs_manual_action`.
- Si `PERCENT_PRICE_BY_SIDE` rechaza → evento `out_of_band` + decisión humana.
- El supervisor cierra manual con `cli supervisor close <bot_id> --reason ...`.

### 2.6 Throttle conservador (50% del límite Binance)
- Weight: 600/min (de 1 200). Orders: 20/10s (de 50).
- Justificación: deja headroom para picos de reconciliación + bursts inesperados.
- `reconcile_server_weight()` permite snapearse al header `X-MBX-USED-WEIGHT-1M` cuando esté cableado el cliente real.

### 2.7 Stub client de primera clase
- `StubLiveClient` no es un mock de testing; es el motor que permite correr el cluster completo `--dry-run` sin red.
- Mismo binario que producción, solo cambia el cliente.
- Habilita el smoke E2E y CI gating antes de cada release.

---

## 3. Smoke E2E - evidencia de validación

`scripts/agartha_cluster_smoke.py` (exit 0 en 5 ciclos):

| Bot | Escenario | Estado final | PnL |
|---:|---|---|---:|
| 1 | Happy path | `closed_win` | +1.999 USDT |
| 2 | Loss path | `closed_loss` | -1.501 USDT |
| 3 | Entry **no se llena** | `awaiting_entry_fill` | — |
| 4 | Exit **no se llena → manual** | `manual_closed` | — |
| 5 | Mid-flight | `in_position` | — |

Métricas del ciclo:
- 9 órdenes en `orders` (5 BUY, 3 SELL filled, 1 cancel/replace por manual close).
- 28 transiciones en `bot_state_log`.
- 50 eventos en `event_log` + JSONL.
- 9 llamadas REST registradas (weight total = 9, latencia stub = 1 ms).
- Throttle al 1.7% de weight budget y 45% de orders budget.

Conclusión: la cobertura de **órdenes que no se llenan** (entries y exits) está validada y produce las trazas esperadas para que el supervisor humano intervenga.

---

## 4. Adeudos al cierre

### 4.1 Único bloqueante para `--live` real (Fase 7 del runbook)
`backtest/agartha_cluster/live_client.py::BinanceAlphaClient` lanza
`NotImplementedError` en 6 métodos. Los endpoints exactos están
documentados en el propio archivo:

| # | Método | Endpoint Binance |
|---|---|---|
| 1 | `get_filters` | `GET /api/v3/exchangeInfo` (cachear 1 día) |
| 2 | `get_price` | `GET /api/v3/ticker/price` (weight 1) |
| 3 | `place_limit` | signed `POST /api/v3/order` (LIMIT, GTC, `newClientOrderId`) |
| 4 | `cancel_order` | signed `DELETE /api/v3/order` |
| 5 | `query_order` | signed `GET /api/v3/order` |
| 6 | `get_account` | signed `GET /api/v3/account` |
| 7 | WS userDataStream | `listenKey` flow → `runner.on_fill(...)` |

Toda respuesta firmada debe llamar `throttle.reconcile_server_weight(used_weight_1m=...)` con el header `X-MBX-USED-WEIGHT-1M`.

Costo estimado: 1–2 días de implementación + harness con testnet Binance.

### 4.2 Deferidos por decisión explícita del operador
- **Dashboard** (HTML estático + DuckDB sobre `cluster.db`): no necesario para arrancar.
- **Alertas externas** (Telegram/Slack): hoy se loggean en `event_log` con `level=critical`. Un sidecar opcional puede leerlos y notificar.

### 4.3 Mejoras incrementales (no bloqueantes)
- ~~Wrapper para promover `best_trial` de Optuna → `set-params` en batch~~
  **Resuelto** (v0.1.1): `cli import-params --batch-json` o
  `--symbol/--study` lee el `best_trial` de Optuna SQLite o del fallback
  `trial_to_run.json` y upserta en bloque. Auto-bootstrap del row de
  `alpha_universe` para satisfacer la FK.
- ~~`load-universe` requiere JSON previo~~ **Resuelto** (v0.1.1): `cli
  load-universe --from-binance` llama directo al endpoint Alpha y filtra
  `offline`/`offsell` por default. `--export-json` permite guardar el
  payload crudo para auditoría.
- Re-optimización rolling automatizada (cron). El state machine ya soporta `optimizing` para un bot ya desplegado; falta el scheduler periódico.
- `--detach` para el `live-up` (envoltura systemd/NSSM externa funciona ya).

---

## 7. Cambios v0.1.1 (2026-05-25, sesión continuación)

### 7.1 `cli load-universe --from-binance`
Refactor de `cmd_load_universe`:
- `--from-json` y `--from-binance` mutuamente excluyentes.
- `_fetch_alpha_token_list_from_binance(...)` filtra `offline`/`offsell` (override con flags).
- Helper `_normalise_universe_row(...)` acepta indistintamente
  `liquidity` / `liquidity_usd`, `alphaId` / `alpha_id`, etc.
- `--limit N` para upserts acotados; `--export-json` para snapshot crudo.

### 7.2 `cli import-params` (nuevo subcomando)
Tres modos:
- `--symbol X --study Y` (single).
- `--batch-json file.json` con lista `[{"symbol":..., "study":...}, ...]`.
- `--storage-path path/to/optuna.db` para override del path convención.

Resolución del best_trial:
1. Optuna SQLite (`<root>/entregables/studies/<study>/optuna.db`).
2. Fallback a `trial_to_run.json` cuando Optuna no está disponible o el path no existe.
3. Auto-upsert mínimo de `alpha_universe` (status=`eligible`) para FK.

### 7.3 Tests añadidos (10 nuevos, suite cluster pasa de 27 → 37)
`tests/test_agartha_cluster_cli_extras.py`:
- `load-universe --from-binance` con filtros, includes, `--limit`, `--export-json`.
- `load-universe --from-json` con normalización de campos.
- `import-params` single via Optuna real.
- `import-params` batch via Optuna real.
- `import-params` fallback a `trial_to_run.json`.
- Errores con mensajes claros (símbolo+estudio faltantes, study inexistente).

---

## 8. Cambios v0.1.2 (2026-05-25, crash-resilience hardening)

Auditoría de persistencia identificó 8 gaps; v0.1.2 cierra los 7 más
relevantes. Solo G7 (purge automatizado de throttle buckets) queda como
helper opcional para que el operador lo agende en su propio cron.

### 8.1 Durabilidad (SQLite)
- `PRAGMA synchronous = FULL` ahora es el default (era NORMAL).
  Configurable por `ClusterDB(path, synchronous="...")`. Tests usan
  NORMAL para velocidad. FULL fsyncea WAL antes de cada commit y antes
  de cada checkpoint, eliminando la ventana de pérdida ante power loss.
- `PRAGMA wal_autocheckpoint = 1000` (~4 MB) acota el tamaño del WAL.
- Nuevo helper `ClusterDB.wal_checkpoint(mode="TRUNCATE")` para
  consolidar el WAL bajo demanda; lo invoca el `ClusterService` cada 30
  min (`wal_checkpoint_every_seconds`) y al final del `recovery_boot`.

### 8.2 Recovery boot
- `ClusterService.start()` ahora invoca `recovery_boot()` automáticamente
  (configurable con `enable_recovery_boot=False`).
- `recovery_boot()` ejecuta 3 pasos:
  1. **Detecta `service_runs` previos** con `stopped_at IS NULL`
     (proceso muerto por SIGKILL / power loss); los marca como
     `crash_detected_on_restart` y emite
     `EventKind.SERVICE_PREVIOUS_CRASH_DETECTED` a nivel `critical`.
  2. **Re-consulta todas las órdenes abiertas** vía
     `Reconciler.poll_open_orders_for_fills()`. Usa la idempotencia
     del `client_order_id` para preguntarle al exchange por cada
     orden `pending`/`submitted` y replayar cualquier fill perdido
     (emite `EventKind.FILL_REPLAYED` warning).
  3. **WAL checkpoint TRUNCATE** para persistir las correcciones
     antes de aceptar nuevo tráfico.
- Cuando el cliente live aún no está cableado, `recovery_boot()`
  detecta `NotImplementedError` en `query_order` y termina ese paso sin
  romper.

### 8.3 Reconciler reforzado
- `Reconciler.poll_open_orders_for_fills()`: para cada orden local en
  estado `submitted`/`partially_filled`/`pending`, llama
  `LiveClient.query_order()` y:
  - `FILLED` → si no hay fill local registrado, replaya `runner.on_fill`
    (idempotente por `client_order_id`); si ya estaba, sólo sincroniza
    estado.
  - `CANCELED` → marca `OrderState.CANCELLED`.
  - `REJECTED`/`EXPIRED` → marca con el estado equivalente.
  - `NEW` → no toca; ya seguirá monitoreándose.
- Se invoca al final de cada `run_once()` (cada 5 min default) **y**
  durante `recovery_boot()`. Cierra el gap de WS-disconnect.

### 8.4 Event logger durable
- `EventLogger(..., fsync_jsonl=True)` (default True). Cada write hace
  `flush()` + `os.fsync()`. Tests pasan `fsync_jsonl=False` para
  velocidad. Coste ~20-30 µs por evento en SSD; despreciable para la
  cadencia del cluster.

### 8.5 Schema sin cambios
La V0001 ya contemplaba `service_runs.stopped_at NULL`, índices y FK.
No requiere migración. Compatible con `cluster.db` de v0.1.0/0.1.1.

### 8.6 Tests añadidos (12 nuevos, suite cluster 37 → 49)
`tests/test_agartha_cluster_recovery.py`:
- R1a: `service_runs` huérfano marcado como crash + evento `critical`.
- R1b: arranque limpio (sin runs previos) no emite ruido de crash.
- R2: orden colgada en `awaiting_entry_fill` resuelve vía `query_order` y bot llega a `in_position` con fill registrado **una sola vez**.
- R3: WS-gap simulado con `force_fill` → reconciler poll replaya el fill; segunda poll es no-op (idempotente).
- R4 (×3): `synchronous=FULL` por default, `NORMAL` cuando se override, garbage rejected.
- R5 (×2): `wal_checkpoint` devuelve counters; modo inválido rechazado.
- R6: `purge_throttle_buckets_older_than` borra el set correcto.
- R7: `fsync_jsonl=True` deja el archivo legible inmediatamente.
- R8: `enable_recovery_boot=False` salta la recuperación.

### 8.7 Garantías que ofrece el cluster post-v0.1.2

| Falla | Comportamiento garantizado |
|---|---|
| Power loss durante write | `synchronous=FULL` + WAL: se pierden 0 transacciones committeadas |
| SIGKILL / crash del proceso | `service_runs` previo se marca; órdenes abiertas se reconcilian al boot |
| Crash entre POST `/order` y update DB | `query_order(client_order_id)` resuelve el estado real al boot |
| WS userDataStream desconectado | Reconciler poll cada 5 min replaya fills perdidos |
| Disco lleno temporal en JSONL | `try/except OSError` en `os.fsync()`; el dato sigue en page cache + DB |
| Doble fill replay | `count_fills_for_order(...)` previene duplicación |
| WAL file crece sin límite | `wal_autocheckpoint=1000` + `wal_checkpoint TRUNCATE` cada 30 min |
| Throttle buckets infinitos | `purge_throttle_buckets_older_than()` para uso en cron |

---

## 5. Workspace al cierre

```
backtest/agartha_cluster/         # 13 módulos, ~3.5k LOC, 27 tests verdes
docs/
  AGARTHA_CLUSTER.md              # diseño canónico
  AGARTHA_CLUSTER_RUNBOOK.md      # guía operador (7 fases)
  AGARTHA_CLUSTER_CHANGELOG.md    # este documento
scripts/
  agartha_cluster_cli.py          # entrypoint CLI
  agartha_cluster_smoke.py        # smoke E2E reutilizable
tests/test_agartha_cluster_*.py   # 4 archivos, 27 tests
```

Suite global del repo: **212 passed, 4 skipped en 5:01** (sin regresiones).

---

## 6. Cómo continuar

1. Implementar `BinanceAlphaClient` (sección 4.1) contra Binance testnet.
2. Repetir Fase 3 del runbook con el cliente real (smoke + `live-up --ticks`).
3. Fase 4 del runbook con `--limit 5` y revisar telemetría 24 h antes de ampliar.
4. Ampliar el universo a velocidad controlada (1 batch/día).
