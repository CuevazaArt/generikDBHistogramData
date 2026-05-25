# Agartha Cluster — diseño, runbook y SOP

Documento canónico del **cluster de bots Agartha** en producción. Un cluster
es un servicio único que despliega y supervisa **N instancias Agartha**
(una por símbolo Alpha) con su seteo óptimo individual, respetando un
**presupuesto de API** y un **ritmo de despliegue controlado**.

Implementación: paquete [`backtest/agartha_cluster/`](../backtest/agartha_cluster/).
Bot base: [`AgarthaStrategy`](../library/bots/agartha/notes.md).

> **¿Eres el operador?** Empieza por la **guía paso a paso**:
> [`AGARTHA_CLUSTER_RUNBOOK.md`](AGARTHA_CLUSTER_RUNBOOK.md). Este documento
> es la referencia de diseño detrás de cada decisión.

---

## 1. Tesis operativa

> Cada bot Agartha es **especialista en un símbolo Alpha** y arriesga un
> capital fijo (10 USDT por defecto). El cluster despliega un bot nuevo
> **cada 10 minutos** tras optimizar sus parámetros vs. la historia
> disponible. El servicio asume:
>
> 1. **Si el cluster corre, la estrategia está activa** sobre los bots ya
>    desplegados; no hay paso manual entre optimización y despliegue.
> 2. El **operador humano** solo garantiza:
>    - **Capital suficiente** en la cuenta spot Alpha (10 USDT por instancia).
>    - **Credenciales válidas** y red estable.
> 3. El cluster respeta **rate limits de Binance** (weight + orders) sin
>    excepción: si la ventana se satura, encola y reintenta.
> 4. Si una `SELL LIMIT` de exit **no se llena** dentro de la ventana
>    tolerada, el cluster intenta **fallbacks autónomos** y, si fallan,
>    eleva alerta al supervisor humano para **cierre manual**.

### Magnitud objetivo

- **Universo Alpha**: ~400 símbolos elegibles (excluye `offline`/`offsell`,
  RWA stock tokens si aplica regla #8).
- **Cadencia de despliegue**: 1 bot cada 10 min ⇒ ~144 bots/día ⇒ universo
  completo en ~3 días continuos.
- **Capital total al 100 %**: 400 × 10 USDT = **~4 000 USDT** en riesgo
  asimétrico (peor caso por bot: token a 0).
- **Cartera estable** (~28 mega-winners según walk-forward): ~280 USDT
  productivos esperados; el resto rota por moonshots fallidos.

---

## 2. Arquitectura

```
┌──────────────────────────────────────────────────────────────────────┐
│                      agartha_cluster_service                          │
│                                                                       │
│   ┌─────────────────┐   ┌──────────────────┐   ┌─────────────────┐    │
│   │ Universe Loader │   │ Optimizer Worker │   │ Deploy Scheduler│    │
│   │  (token list +  │──▶│ (optuna 100t per │──▶│ (1 cada 10 min, │    │
│   │   filters)      │   │  symbol)         │   │  throttle aware)│    │
│   └─────────────────┘   └──────────────────┘   └────────┬────────┘    │
│                                                          │             │
│                       ┌──────────────────────────────────▼──────────┐ │
│                       │             Bot Runner (uno por bot vivo)   │ │
│                       │   place_entry → await_fill → trailing →     │ │
│                       │   place_exit → await_fill → close cycle     │ │
│                       └──────────┬────────────────┬──────────────────┘ │
│                                  │                │                    │
│                       ┌──────────▼──┐    ┌────────▼─────────┐         │
│                       │ Live Client │    │ Fill Watcher /   │         │
│                       │ (REST + WS  │◀──▶│ Exit Supervisor  │         │
│                       │  user-data) │    │ (stale orders)   │         │
│                       └──────┬──────┘    └────────┬─────────┘         │
│                              │                    │                    │
│                       ┌──────▼────────────────────▼──────────────┐    │
│                       │  API Throttle  +  Event Logger  +  DAO   │    │
│                       └────────────────────┬─────────────────────┘    │
└────────────────────────────────────────────┼──────────────────────────┘
                                              │
                                  ┌───────────▼────────────┐
                                  │  cluster.db (SQLite)   │
                                  │  WAL, single file      │
                                  └────────────────────────┘
                                  + logs/agartha_cluster/  (JSONL telemetría)
```

### Componentes (uno por archivo en `backtest/agartha_cluster/`)

| Archivo | Rol |
|---|---|
| `models.py` | Enums + dataclasses (BotState, OrderState, EventKind, etc.). |
| `cluster_db.py` | DAO sobre SQLite WAL; migraciones idempotentes. |
| `state_machine.py` | Transiciones válidas del bot; rechazo defensivo. |
| `api_throttle.py` | Budget rolling (weight + orders); espera hasta liberar. |
| `event_logger.py` | Inserta eventos estructurados en DB y stdout. |
| `scheduler.py` | Cola FIFO; despacha 1 bot/10 min respetando throttle. |
| `credentials.py` | OS keyring; prompt sólo en `live up`. |
| `live_client.py` | Interface `LiveClient` + `StubLiveClient` y `BinanceAlphaClient` (este último marcado TODO hasta credenciales). |
| `bot_runner.py` | Ciclo de vida de un bot (asíncrono). |
| `reconciler.py` | Snapshot periódico de open orders y balance. |
| `cluster_service.py` | Loop principal asyncio; shutdown limpio. |
| `cli.py` | Subcomandos del CLI (consumido por `scripts/agartha_cluster_cli.py`). |

---

## 3. Schema de la base de datos

SQLite en `cluster.db` (configurable). Modo **WAL** para lecturas concurrentes
desde dashboards / supervisor sin bloquear escrituras del servicio.

| Tabla | Rol |
|---|---|
| `alpha_universe` | Símbolos candidatos + estado (`eligible`, `studied`, `deployed`, `blacklist`). |
| `symbol_params` | Mejores parámetros por símbolo (resultado de Optuna). |
| `symbol_filters` | `tick_size`, `PERCENT_PRICE_BY_SIDE`, `min_notional` cacheados por símbolo. |
| `cluster_bots` | Una fila por bot desplegado. Estado + snapshot de params. |
| `bot_state_log` | Historial completo de transiciones de estado por bot. |
| `deploy_queue` | Ítems planificados; el scheduler los consume FIFO. |
| `orders` | Toda orden enviada (entry + exit + cancel + reorder). |
| `fills` | Fills recibidos desde `userDataStream`. |
| `api_calls` | Log REST: endpoint, weight, status, latency. |
| `event_log` | Eventos estructurados (`source`, `kind`, `level`, `payload`). |
| `api_throttle_buckets` | Conteo rolling de weight/orders por minuto. |
| `reconciliation_snapshots` | Snapshots periódicos del estado de la cuenta. |
| `service_runs` | Inicio/fin del proceso servicio. |
| `credentials_meta` | Puntero a OS keyring (jamás plaintext). |

Esquema completo: [`backtest/agartha_cluster/migrations/V0001__cluster_schema.sql`](../backtest/agartha_cluster/migrations/V0001__cluster_schema.sql).

---

## 4. Ciclo de vida del bot (state machine)

```
created
  ↓  optimizer pickup
optimizing
  ↓  optuna ok                 ↓ optuna fail
optimized                      failed_optimization
  ↓  scheduler tick (10 min throttle)
queued
  ↓  api budget ok
placing_entry
  ↓  REST OK                   ↓ REST fail (retry)
awaiting_entry_fill
  ↓  fill WS                   ↓ timeout / canceled
in_position                    cancelled_entry
  ↓  trailing trigger
placing_exit
  ↓  REST OK
awaiting_exit_fill
  ↓  fill           ↓ stale (no fill > N min)
closed_win/loss    stale_exit
                    ↓  fallback path (re-quote at band)
                    ↓  ↳ supervisor manual close → manual_closed
```

Transiciones inválidas se rechazan en `state_machine.transition()`.

---

## 5. Throttle de API

Binance spot mainnet: 1 200 weight/min y 50 orders/10 s; Alpha comparte límite.

Política del cluster (conservadora):
- **Weight budget**: 600/min (50 % del límite, deja headroom).
- **Orders budget**: 20/10 s.
- **Deploy budget extra**: máximo 1 deploy por slot de 10 min ⇒ máximo
  2 órdenes/10 min por deploy (entry + posible reorder), insignificante.

El throttle bloquea hasta que la ventana libera; emite evento
`api_throttle_wait` con el tiempo de espera.

---

## 6. Manejo de fallos de SELL LIMIT (regla principal del usuario)

Cuando el trailing dispara, el bot envía una `SELL LIMIT` calculada por
`agartha_exit_planner.plan_exit()`.

| Caso | Acción del cluster |
|---|---|
| Llena en < 60 s | `closed_win/loss`; emite evento `exit_filled`. |
| No llena en 60 s, dentro de banda | Cancela + reenvía LIMIT al **bid actual** (más agresivo). Evento `exit_reorder`. |
| Aún no llena en 5 min | Cancela + LIMIT al **borde inferior de banda** (`TRAIL_BORDER`). Evento `exit_border`. |
| Aún no llena en 10 min | Marca bot `stale_exit`; emite **alerta supervisor** (`needs_manual_action`). |
| `PERCENT_PRICE_BY_SIDE` rechaza | Evento `out_of_band`; el supervisor decide cierre manual o esperar. |
| Error 5xx repetido | Backoff exponencial; tras 3 intentos eleva alerta. |

El **supervisor** (humano) usa `cli supervisor close <bot_id>` para forzar
un cierre LIMIT al bid actual; queda registrado como `manual_closed`.

Cada uno de estos eventos se persiste en `event_log` con `source =
"service"` o `source = "binance_rest"` y un `correlation_id` que liga la
serie completa de intentos.

---

## 7. Trazabilidad (logs + DB)

Cada acción genera **dos registros**:

1. **`event_log` row** en `cluster.db` (estructurada, queryable).
2. **JSONL append-only** en `logs/agartha_cluster/<YYYY-MM-DD>.jsonl`
   (forensia, replicable a S3/Slack/etc.).

El `correlation_id` (uuid4) liga: deploy → entry order → entry fill →
trailing decisions → exit order → exit fill (o stale path completo).

API calls REST tienen su propio sub-log (`api_calls` table) con: endpoint,
weight consumido, status code, latencia y `correlation_id`.

---

## 8. Credenciales

- **Storage**: OS keyring (Windows `CredentialManager`; macOS `Keychain`;
  Linux `Secret Service`) vía paquete `keyring` (dependencia opcional).
- **Solicitud**: solo en `cli live up` la primera vez. Prompt seguro
  (`getpass`). Guarda `api_key` y `api_secret` con `service_name =
  "binance_alpha"` y `username = "<perfil>"`.
- **Verificación**: `cli creds check` hace `GET /account` (signed) y
  reporta solo `canTrade` / `accountType`; nunca persiste el response.
- **Rotación**: `cli creds rotate` borra entrada del keyring y vuelve a
  pedir.
- **DB**: solo guarda **puntero** en `credentials_meta` (service_name +
  username + timestamp de creación). Nunca el secreto.

---

## 9. CLI (operación diaria)

```powershell
# Inicialización (una vez)
python scripts/agartha_cluster_cli.py init-db
python scripts/agartha_cluster_cli.py load-universe --refresh

# Optimizar y encolar los top-N por liquidez/holders
python scripts/agartha_cluster_cli.py schedule-batch --top 200 --by holders

# Inspección
python scripts/agartha_cluster_cli.py status
python scripts/agartha_cluster_cli.py status --bot 42
python scripts/agartha_cluster_cli.py report SYMBOL

# Arrancar el servicio (prompts credenciales si faltan)
python scripts/agartha_cluster_cli.py live up

# Supervisor
python scripts/agartha_cluster_cli.py supervisor list-stale
python scripts/agartha_cluster_cli.py supervisor close 42 --reason "manual close, exit_planner OOB"

# Parar
python scripts/agartha_cluster_cli.py live down
```

`live up` corre el servicio en foreground por default; `--detach` (futuro)
lo lanza como tarea systemd/NSSM.

---

## 10. Modo dry-run (sin credenciales)

`cli live up --dry-run` arranca el servicio entero pero usa
`StubLiveClient`: no envía órdenes reales, simula fills con curva sintética
y registra todo en la DB. Útil para validar el wiring del scheduler,
throttle, state machine y event logger antes de tocar producción.

---

## 11. SOP del supervisor

| Situación | Acción |
|---|---|
| Bot en `stale_exit` > 10 min | `supervisor close <bot_id>`. Revisar `event_log` para causa. |
| Alerta `out_of_band` repetida en mismo símbolo | Considerar `blacklist <symbol>` (no se vuelve a desplegar). |
| Reconciler reporta open order sin bot asociado | Investigar: posible orden manual previa. Si es huérfana, cancelar con `supervisor cancel-order <order_id>`. |
| `service_runs` muestra crashes repetidos | Revisar `event_log WHERE level='critical'` y stdout JSONL. |
| Cap de capital alcanzado | Detener `live`; aumentar capital en cuenta; reanudar. |

---

## 12. Pendientes hasta puesta en producción

1. **Conector live real** (`live_client.BinanceAlphaClient`): endpoints
   REST signed + `userDataStream` WS. Marcado `NotImplementedError` hasta
   que el operador inicie `live up` y provea credenciales.
2. **Dashboard** ligero (HTMl static + DuckDB sobre `cluster.db`): no
   necesario para arrancar, pero recomendado.
3. **Alertas externas** (Telegram/Slack): hoy se loggea a `event_log`
   nivel `critical`; un sidecar lector puede notificarlas.
4. **Re-optimización rolling**: cron semanal que reabre `optimizing` para
   los símbolos ya desplegados; soportado por el state machine pero sin
   schedule en este snapshot.
