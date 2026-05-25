# Agartha Cluster - Runbook del operador

Esta es la **guía paso a paso** para dejar el cluster Agartha funcionando.
Para diseño, arquitectura y SOPs de incidentes ver
[`AGARTHA_CLUSTER.md`](AGARTHA_CLUSTER.md).

Implementación: `backtest/agartha_cluster/`.
CLI: `python scripts/agartha_cluster_cli.py <subcomando>`.

---

## TL;DR (flujo en 6 fases)

```
F0 Prerequisitos -> F1 Setup una vez -> F2 Datos+params por simbolo
   -> F3 Smoke (dry-run) -> F4 Lanzamiento live -> F5 Operacion diaria
```

> **Estado actual (mayo 2026):** el `BinanceAlphaClient` real todavía no
> está conectado (queda como `NotImplementedError`). Hasta que se cablée
> esa pieza el cluster solo corre en **modo `--dry-run`** con `StubLiveClient`.
> Lo demás (DB, scheduler, throttle, runner, supervisor, telemetría,
> **recovery-boot post-crash**) está productivo y cubierto por 49 tests +
> smoke E2E. Ver [Fase 7](#fase-7---cuando-se-cablée-el-cliente-live-real)
> abajo.

---

## Fase 0 - Prerrequisitos del operador

### 0.1 Software
- Python 3.11 (el repo se prueba en `cpython-311`).
- Dependencias del repo instaladas (`pip install -r requirements.txt` si
  aplica; o `pip install keyring` si no está aún para storage de
  credenciales en OS keyring).
- Windows / macOS / Linux: el cluster es OS-agnóstico; los ejemplos del
  runbook usan PowerShell.

### 0.2 Cuenta Binance Alpha
- **API key + secret** con permisos:
  - `Enable Reading`: ON (requerido)
  - `Enable Spot Trading`: ON (requerido)
  - `Enable Withdrawals`: **OFF** (regla de seguridad del operador)
  - IP whitelist opcional pero recomendada.
- La estrategia del cluster acepta solo órdenes `LIMIT` (Binance Alpha no
  ofrece `STOP_LOSS` ni `OCO`; ver `library/bots/agartha/notes.md`
  sección "Decisión técnica").

### 0.3 Capital
- Default: **10 USDT por bot**.
- Universo Alpha ~400 símbolos -> máximo teórico ~4 000 USDT en riesgo
  asimétrico.
- Para empezar usar batch acotado: `--limit 20` (≈ 200 USDT comprometidos)
  hasta tener confianza en el conector live.

### 0.4 Histórico para optimización
- El cluster optimiza parámetros por símbolo **fuera del servicio**, usando
  `scripts/agartha_optuna_spectrum.py` (o `agartha_walkforward.py`) sobre
  klines descargadas con `scripts/download_and_prepare_alpha.py`.
- No es necesario tener histórico al instante para los 400 símbolos: el
  operador puede empezar con un subset, optimizar, encolar, y ampliar.

---

## Fase 1 - Setup una sola vez

Todo lo de esta fase queda persistido en `cluster.db` (SQLite WAL) y no se
repite en futuros arranques.

### 1.1 Inicializar la DB

```powershell
python scripts/agartha_cluster_cli.py init-db
```

Salida esperada:
```
cluster.db initialised at C:\...\cluster.db (schema v0001)
```

Esto aplica el script `backtest/agartha_cluster/migrations/V0001__cluster_schema.sql`
de forma idempotente (puede correrse N veces sin efectos secundarios).

### 1.2 Guardar credenciales en OS keyring

```powershell
python scripts/agartha_cluster_cli.py creds set --profile default
```

Pide la API Key y el API Secret de forma interactiva (el secret nunca se
echo-ea). Persiste en:
- Windows: Credential Manager (`binance_alpha:default`)
- macOS: Keychain
- Linux: Secret Service

La DB **solo** guarda el puntero (`service_name`, `username`,
`storage_method`), nunca el secreto. Verificar con:

```powershell
python scripts/agartha_cluster_cli.py creds check --profile default
```

> **Fallback sin keyring:** si la instalación de `keyring` no es posible,
> exportar `BINANCE_ALPHA_KEY` y `BINANCE_ALPHA_SECRET` como variables de
> entorno antes de lanzar `live-up`. El servicio las detecta
> automáticamente. **Nunca** commitear estas variables al repo.

### 1.3 Cargar el universo Alpha

El cluster necesita la lista de símbolos elegibles. Hay dos opciones:

**(a) Directo desde Binance Alpha (recomendado):**

```powershell
python scripts/agartha_cluster_cli.py load-universe --from-binance
```

Llama `GET /bapi/.../alpha/all/token/list` y filtra `offline`/`offsell`
por default. Flags útiles:
- `--include-offline` / `--include-offsell`: no filtrar (debug).
- `--limit N`: solo upsertar los primeros N (sanity check).
- `--export-json data/alpha_universe.json`: guardar también el payload
  crudo para auditoría / re-import offline.

**(b) Desde un JSON previamente descargado:**

```powershell
python scripts/agartha_cluster_cli.py load-universe --from-json data/alpha_universe.json
```

Formato esperado (array de objetos):

```json
[
  {"symbol": "FOOUSDT", "alpha_id": "ALPHA_953USDT", "quote_asset": "USDT",
   "status": "eligible", "holders": 12345, "liquidity_usd": 100000},
  ...
]
```

Los campos `liquidity` y `liquidity_usd` son aceptados indistintamente; el
CLI los normaliza al schema interno.

Verificar:
```powershell
python scripts/agartha_cluster_cli.py status
# (no muestra bots todavía; ese listado se llena después de schedule-batch)
```

---

## Fase 2 - Datos históricos y parámetros por símbolo

Esta fase corre **fuera del servicio**, una vez por símbolo (y se repite
periódicamente para re-optimización rolling).

### 2.1 Descargar histórico

Para cada símbolo del universo activo:

```powershell
python scripts/download_and_prepare_alpha.py --symbol FOOUSDT --interval 15m
```

El histórico queda persistido en `klines.db` + dataset artifact bajo
`reports/entregables/datasets/<name>/manifest.json`. La descarga es
incremental (reanuda desde el último kline).

### 2.2 Optimizar parámetros

Usando Optuna spectrum (recomendado para Alpha por la asimetría):

```powershell
python scripts/agartha_optuna_spectrum.py `
  --study agartha_FOOUSDT_15m `
  --trials 100 --extreme 20 `
  --initial_cash 10 --quote_order_qty_usdt 10 `
  --start_ts <ts_inicio_ms> --end_ts <ts_fin_ms>
```

Resultado: un estudio Optuna persistido + un `spectrum.png` + el `best_trial`
con los parámetros óptimos (`trailing_stop_pct`, `activation_profit_pct`,
`breakeven_lock_pct`, `entry_limit_offset_pct`, etc.).

### 2.3 Promover los parámetros al cluster

**Opción A — Importar directamente el `best_trial` de Optuna (recomendado):**

```powershell
# Un solo símbolo
python scripts/agartha_cluster_cli.py import-params `
  --symbol FOOUSDT --study agartha_FOOUSDT_15m --root reports
```

Lee el mejor trial del estudio Optuna (resuelve
`<root>/entregables/studies/<study>/optuna.db` por convención) y upserta
a `symbol_params` con los 4 parámetros y la trazabilidad
(`study_trial_id`, `study_equity_pct`, `optuna_db_path`,
`optimized_at`). Si la fila de `alpha_universe` no existe aún para ese
símbolo, se crea con status `eligible` automáticamente (FK satisfecha).

Para promover **N estudios en batch**:

```powershell
python scripts/agartha_cluster_cli.py import-params `
  --batch-json data/optuna_promotions.json --root reports
```

Con `data/optuna_promotions.json`:
```json
[
  {"symbol": "FOOUSDT", "study": "agartha_FOOUSDT_15m"},
  {"symbol": "BARUSDT", "study": "agartha_BARUSDT_15m"},
  ...
]
```

Si `optuna.db` no se encuentra en el path convención, el CLI cae a
`trial_to_run.json` (mismo directorio del estudio); útil cuando el
estudio fue movido o `optuna` no está instalado en el host del cluster.

**Opción B — Set manual (ad-hoc, debugging):**

```powershell
python scripts/agartha_cluster_cli.py set-params FOOUSDT `
  --trailing 25.0 --activation 0.0 --breakeven 0.0 --entry-offset 0.0
```

Cualquiera de las dos rutas termina en una fila de `symbol_params`. Sin
esa fila el scheduler **rechaza el deploy** del símbolo con
`reason=no_params; run optimizer first`.

---

## Fase 3 - Smoke test antes de tocar producción

Validar que el wiring completo (scheduler, throttle, runner, state
machine, event log, persistencia) funciona en tu máquina **sin red**.

### 3.1 Smoke E2E con escenarios mixtos

```powershell
python scripts/agartha_cluster_smoke.py
```

Despliega 5 bots con `StubLiveClient` y cubre las ramas críticas:
- Entry fills + trailing + exit fills (WIN y LOSS)
- Entry **no se llena** (precio se aleja del LIMIT)
- Exit **no se llena** -> supervisor manual close
- Bot mid-flight aún en posición

Sale `exit 0` si los 5 escenarios cumplen su expectativa y deja artefactos
(SQLite + JSONL + ledger de órdenes + breakdown de eventos) en
`%TEMP%\agartha_smoke_<rand>`.

### 3.2 Dry-run del servicio real (mismo binario que producción)

Corre el `ClusterService` exactamente como en live, pero con
`StubLiveClient` y N ticks:

```powershell
python scripts/agartha_cluster_cli.py live-up --dry-run --ticks 20
```

Inspeccionar después:
```powershell
python scripts/agartha_cluster_cli.py status
python scripts/agartha_cluster_cli.py report FOOUSDT
```

---

## Fase 4 - Lanzamiento live (cuando el conector real esté cableado)

### 4.1 Sanidad previa

```powershell
python scripts/agartha_cluster_cli.py creds check --profile default
python scripts/agartha_cluster_cli.py status
```

Verificar:
- Credenciales presentes.
- DB inicializada.
- Universo cargado.
- Al menos 1 fila en `symbol_params` por cada símbolo que se vaya a
  encolar.

### 4.2 Encolar un batch acotado

**Recomendado en primer arranque**: 5-10 símbolos para validar.

```powershell
python scripts/agartha_cluster_cli.py schedule-batch `
  --status eligible --limit 5 --slot-seconds 600 --priority 100
```

- `--slot-seconds 600` = 1 deploy cada 10 min (default del diseño).
- `--priority` controla orden FIFO si hay items conflictivos.

### 4.3 Arrancar el servicio

```powershell
python scripts/agartha_cluster_cli.py live-up --capital-usdt 10 --slot-seconds 600
```

Foreground por default. El proceso:
1. Carga credenciales del keyring (o env vars si no hay keyring).
2. Instancia el `BinanceAlphaClient` real.
3. Loggea `service_start` en `event_log`.
4. Cada `tick_seconds` (5s por default):
   - Despliega 1 bot si hay slot abierto + budget de API.
   - Avanza el estado de cada bot vivo (chequea trailing, coloca exits).
   - Cada `reconcile_every_seconds` (300s) corre el reconciliador.
5. `Ctrl+C` -> shutdown limpio: marca `service_runs` con `stopped_reason=keyboard_interrupt`.

> **Detach:** la versión actual corre en foreground. Para producción 24/7
> envolver en NSSM (Windows) o un unit de systemd (Linux). El servicio es
> **idempotente en restart**: lee `cluster_bots` de la DB y continúa cada
> bot desde donde quedó.

---

## Fase 5 - Operación diaria

### 5.1 Inspección

```powershell
# Lista los últimos N bots
python scripts/agartha_cluster_cli.py status --limit 50

# Reporte detallado de un símbolo (todos sus deployments)
python scripts/agartha_cluster_cli.py report FOOUSDT --limit 10
```

### 5.2 Atender bots colgados

```powershell
# Listar bots que pasaron a STALE_EXIT (10+ min sin fill en el exit)
python scripts/agartha_cluster_cli.py supervisor list-stale

# Forzar cierre manual (cancela LIMIT pendiente, envia SELL LIMIT @ bid-10bps)
python scripts/agartha_cluster_cli.py supervisor close 42 `
  --reason "exit OOB tras pump abrupto; cierro a mercado tolerable"
```

El cierre manual queda registrado:
- `bot_state_log`: transición a `manual_closed` con `reason`.
- `event_log`: evento `manual_close`, `level=warning`, `source=supervisor`.
- `orders`: nueva orden SELL marcada con prefix `agc-mcls-`.

### 5.3 Indicadores que el operador debe monitorear

| Indicador | Cómo verlo | Acción si dispara |
|---|---|---|
| Bots en `stale_exit` | `cli supervisor list-stale` | Investigar evento + cerrar manual |
| Crashes repetidos del servicio | `SELECT * FROM service_runs ORDER BY started_at DESC LIMIT 20` | Revisar `event_log WHERE level IN ('critical','error')` |
| Throttle saturado | `event_log` filtrando `kind='api_throttle_wait'` | Reducir batch o subir budget de la cuenta |
| Reconciler drift | `event_log` con `kind='reconciliation_drift'` | Verificar que no haya órdenes manuales externas |
| PnL agregado | `SELECT SUM(realized_pnl_usdt) FROM cluster_bots WHERE state IN ('closed_win','closed_loss','manual_closed')` | Tracking semanal |

### 5.4 Re-optimización rolling

Cuando un símbolo lleva > 1 semana desplegado (o cada N días por política),
repetir Fase 2 con datos frescos y volver a llamar `set-params`. El
servicio recoge los nuevos params en el **siguiente deploy** del símbolo,
no en el bot vivo (que mantiene su snapshot inmutable hasta cerrar).

---

## Fase 6 - Apagado y mantenimiento

### 6.1 Apagado limpio

`Ctrl+C` en la terminal del `live-up`. El servicio:
- Setea flag interno `_stop=True`.
- Cierra `service_runs` con `stopped_reason`.
- Emite evento `service_stop`.
- **Los bots vivos quedan en sus estados intermedios** (`awaiting_entry_fill`,
  `in_position`, `awaiting_exit_fill`) y se reanudan en el próximo `live-up`.

### 6.2 Reanudación

```powershell
python scripts/agartha_cluster_cli.py live-up
```

Como `cluster_bots` y `orders` viven en DB, el runner ve los bots
existentes y continúa. Las órdenes `submitted` se siguen monitoreando vía
WS userDataStream. El reconciler corrige cualquier drift causado por
fills/cancels que ocurrieron mientras el proceso estuvo abajo.

### 6.2.1 Recovery boot (v0.1.2+)

Cada vez que el servicio arranca ejecuta **`recovery_boot()` automáticamente**
antes del primer tick. Lo que hace y qué esperar en cada caso:

| Escenario | Qué hace el recovery_boot | Qué ves en `event_log` |
|---|---|---|
| Apagado limpio anterior | nada anómalo | 1 evento `service_recovery_started` + `service_recovery_completed` con counters en 0 |
| Crash / SIGKILL / power loss | marca el `service_runs` previo como `crash_detected_on_restart` | 1 evento `service_previous_crash_detected` nivel **critical** con `prev_run_id`, `prev_pid`, `host` |
| Crash con orden colgada en `pending` | llama `query_order(client_order_id)`; si el exchange dice `FILLED`, replaya `on_fill` y avanza el bot a `in_position` (idempotente) | eventos `order_requeried` por cada orden + `fill_replayed` (warning) por cada fill recuperado |
| WS estuvo caído antes del crash | mismo mecanismo: `query_order` cierra el gap | `order_requeried` + `fill_replayed` |
| Cliente live aún no cableado | el paso de re-query se omite silenciosamente (`NotImplementedError`) | counters en `service_recovery_completed` quedan en 0 |

> **Garantías post-crash:**
> 1. **Cero órdenes duplicadas** — `client_order_id` es UNIQUE y determinista.
> 2. **Cero fills duplicados** — `count_fills_for_order(...)` previene replay doble.
> 3. **Cero transacciones DB perdidas ante power loss** — `PRAGMA synchronous=FULL` + WAL fsyncea antes de cada commit.
> 4. **Service runs previos siempre marcados** — `stopped_at` se actualiza al boot siguiente aunque el proceso anterior haya muerto sin ejecutar el handler de shutdown.
> 5. **Eventos forenses JSONL persistentes** — cada write se `fsync()`ea (~30 µs por evento).

Para verificar manualmente lo que pasó en el último arranque:

```powershell
sqlite3 cluster.db "SELECT level, kind, payload_json FROM event_log WHERE kind IN ('service_recovery_started','service_recovery_completed','service_previous_crash_detected','fill_replayed','order_requeried') ORDER BY event_id DESC LIMIT 20"
```

Si por alguna razón quieres **desactivar** el recovery boot (NO recomendado):

```python
ServiceConfig(enable_recovery_boot=False)
```

### 6.2.2 Resiliencia a degradación de red

| Falla | Mecanismo |
|---|---|
| WS userDataStream timeout | reconciler corre cada 5 min (`reconcile_every_seconds=300`) y hace `query_order` por cada orden local abierta; replaya fills perdidos |
| REST 5xx / timeout en `place_limit` | el orden ya tiene `client_order_id` en DB con `state=pending`; al siguiente recovery boot o reconcile, `query_order` resuelve si entró |
| Conexión perdida largo rato | al volver, el primer reconcile + el recovery boot del próximo restart cierran cualquier gap |
| Disco temporalmente lleno (JSONL) | `os.fsync` lanza `OSError` ignorado; el dato vive en DB y page cache del OS; al liberarse, la siguiente escritura completa flushea |

### 6.2.3 Mantenimiento de la DB

| Tarea | Frecuencia | Comando |
|---|---|---|
| WAL checkpoint forzado | Automático cada 30 min mientras corre `live-up`; manual: | `python -c "from backtest.agartha_cluster.cluster_db import ClusterDB; d=ClusterDB('cluster.db'); print(d.wal_checkpoint(mode='TRUNCATE'))"` |
| Purga de `api_throttle_buckets` (>1 día) | Semanal (cron) | `python -c "from backtest.agartha_cluster.cluster_db import ClusterDB; import time; d=ClusterDB('cluster.db'); cutoff=int(time.time()//60)-1440; print(d.purge_throttle_buckets_older_than(before_minute_bucket=cutoff))"` |
| Backup atómico | Diario | `python -c "import sqlite3; c=sqlite3.connect('cluster.db'); c.execute(\"VACUUM INTO 'backups/cluster.db.bak'\"); c.close()"` |

### 6.3 Backup

`cluster.db` es **único punto de verdad** persistido. Backup recomendado:
- Snapshot diario con WAL checkpoint:
  ```powershell
  python -c "import sqlite3; c=sqlite3.connect('cluster.db'); c.execute('VACUUM INTO ?', ('backups/cluster.db.bak',)); c.close()"
  ```
- JSONL de `logs/agartha_cluster/` también se archiva (append-only,
  rotación diaria).

### 6.4 Blacklist de símbolos problemáticos

Si un símbolo causa repetidos `out_of_band` o `needs_manual_action`:

```sql
UPDATE alpha_universe SET status='blacklist' WHERE symbol='BADUSDT';
```

El próximo `schedule-batch --status eligible` lo omitirá.

---

## Fase 7 - Cuando se cablée el cliente live real

El único bloqueante pendiente para producción es
`backtest/agartha_cluster/live_client.py::BinanceAlphaClient`. Las 7
operaciones a implementar (con su endpoint) están documentadas en el mismo
archivo:

1. `get_filters` -> `GET /api/v3/exchangeInfo` (cachear 1 día).
2. `get_price` -> `GET /api/v3/ticker/price` (weight 1).
3. `place_limit` -> signed `POST /api/v3/order` (type=LIMIT, GTC, `newClientOrderId`).
4. `cancel_order` -> signed `DELETE /api/v3/order`.
5. `query_order` -> signed `GET /api/v3/order`.
6. `get_account` -> signed `GET /api/v3/account`.
7. WS `userDataStream` (`listenKey` flow) para `executionReport` -> llama
   `runner.on_fill(...)` por cada fill recibido.

Toda respuesta firmada debe pasar
`throttle.reconcile_server_weight(used_weight_1m=...)` con el header
`X-MBX-USED-WEIGHT-1M`.

Tras cablear, repetir Fase 3 (smoke + dry-run) y luego Fase 4 con batch
acotado.

---

## Checklist resumido (imprimible)

```
[ ] F0  python --version (3.11.x)
[ ] F0  pip install keyring
[ ] F0  API key Alpha con READ+SPOT, sin WITHDRAW
[ ] F0  Capital >= 10 USDT * (# símbolos a desplegar)
[ ] F1  cli init-db
[ ] F1  cli creds set --profile default
[ ] F1  cli creds check
[ ] F1  cli load-universe --from-binance  (o --from-json)
[ ] F2  download_and_prepare_alpha.py por símbolo
[ ] F2  agartha_optuna_spectrum.py por símbolo
[ ] F2  cli import-params --batch-json data/optuna_promotions.json
        (o set-params manual por símbolo)
[ ] F3  python scripts/agartha_cluster_smoke.py  (exit 0)
[ ] F3  cli live-up --dry-run --ticks 20
[ ] F4  cli schedule-batch --limit 5  (batch pequeño primero)
[ ] F4  cli live-up
[ ] F5  cli status / report / supervisor list-stale (diariamente)
[ ] F5  Re-optimizar params cada semana
[ ] F6  Backup cluster.db diariamente
[ ] F7  Wire BinanceAlphaClient (7 endpoints) cuando esté en producción
```

---

## Atajos al diseño detrás

- Diagrama de arquitectura, schema DB completo, state machine, throttle,
  manejo de fallos SELL: [`AGARTHA_CLUSTER.md`](AGARTHA_CLUSTER.md).
- Tesis del bot base (sin SL, trailing dinámico, asimetría):
  [`library/bots/agartha/notes.md`](../library/bots/agartha/notes.md).
- Smoke E2E reutilizable: [`scripts/agartha_cluster_smoke.py`](../scripts/agartha_cluster_smoke.py).
- Tests del cluster: `tests/test_agartha_cluster_*.py` (27 tests, 2.58 s).
