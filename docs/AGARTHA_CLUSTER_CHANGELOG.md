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
- Wrapper para promover `best_trial` de Optuna → `set-params` en batch (hoy es manual por símbolo).
- Re-optimización rolling automatizada (cron). El state machine ya soporta `optimizing` para un bot ya desplegado; falta el scheduler periódico.
- `--detach` para el `live-up` (envoltura systemd/NSSM externa funciona ya).

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
