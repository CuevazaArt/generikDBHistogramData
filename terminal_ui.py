"""Terminal UI para explorar la DB local y solicitar descargas de datos Binance."""
import sys
from typing import Optional, List
from db import init_db, query_klines, insert_klines
import sqlite3
from binance_hist_downloader import BinanceDownloader
from datetime import datetime
import time
from tqdm import tqdm
import logging
import logging.handlers
import os
import json
import requests
import socket

# Setup logging
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "terminal_ui.log")
logger = logging.getLogger("terminal_ui")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    # also log to console
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    # JSON lines handler for ingestion
    json_log = os.path.join(LOG_DIR, "terminal_ui.jsonl")
    jhandler = logging.handlers.RotatingFileHandler(json_log, maxBytes=10_000_000, backupCount=3, encoding="utf-8")

    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload = {
                "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                "level": record.levelname,
                "message": record.getMessage(),
            }
            # include extra attributes if present
            if hasattr(record, "args") and record.args:
                try:
                    payload["args"] = record.args
                except Exception:
                    pass
            return json.dumps(payload, ensure_ascii=False)

    jhandler.setFormatter(JsonFormatter())
    logger.addHandler(jhandler)

DB_PATH = "klines.db"


def normalize_symbol(value: str) -> str:
    return (value or "").strip().upper()


def normalize_interval(value: str) -> str:
    return (value or "").strip().lower()


def normalize_epoch_ms(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    try:
        ts = int(value)
    except Exception:
        return None
    # Accept ms as canonical; if value looks like micro/nanoseconds convert down.
    while abs(ts) > 10_000_000_000_000:
        ts //= 1000
    return ts


def format_ts(ms: Optional[int]) -> str:
    normalized = normalize_epoch_ms(ms)
    if normalized is None:
        return "-"
    try:
        return datetime.utcfromtimestamp(int(normalized) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


def get_series_row_count(db_path: str, symbol: str, interval: str) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM klines WHERE symbol=? AND interval=?", (symbol, interval))
    row = cur.fetchone()
    conn.close()
    return int(row[0] or 0)


def explain_download_error(exc: Exception, operation: str) -> str:
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        status = response.status_code if response is not None else None
        if status == 400:
            return f"{operation}: solicitud inválida (HTTP 400). Revisa símbolo, intervalo y rango de fechas."
        if status == 404:
            return f"{operation}: recurso no encontrado (HTTP 404). Puede que no exista data para ese símbolo/intervalo/mes."
        if status == 429:
            return f"{operation}: límite de peticiones alcanzado (HTTP 429). Intenta más tarde o reduce ritmo de consultas."
        if status is not None:
            return f"{operation}: error HTTP {status}. Detalle: {exc}"
        return f"{operation}: error HTTP. Detalle: {exc}"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"{operation}: timeout de red. Verifica conexión o intenta de nuevo."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return f"{operation}: no se pudo conectar a Binance/data.binance.vision. Revisa internet o firewall."
    return f"{operation}: {exc}"


def print_error(message: str) -> None:
    print("\n[ERROR]")
    print(message)


def run_diagnostics(db_path: str) -> None:
    print("\n=== Diagnostico del sistema ===")
    checks = []

    # DB check
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='klines'")
        has_klines = int(cur.fetchone()[0] or 0) > 0
        total_rows = 0
        if has_klines:
            cur.execute("SELECT COUNT(*) FROM klines")
            total_rows = int(cur.fetchone()[0] or 0)
        conn.close()
        checks.append(("DB accesible", "OK", f"{db_path} | klines={'si' if has_klines else 'no'} | filas={total_rows:,}"))
    except Exception as exc:
        checks.append(("DB accesible", "FAIL", str(exc)))

    # DNS checks
    for host in ("api.binance.com", "data.binance.vision"):
        try:
            ip = socket.gethostbyname(host)
            checks.append((f"DNS {host}", "OK", ip))
        except Exception as exc:
            checks.append((f"DNS {host}", "FAIL", str(exc)))

    # HTTP checks
    try:
        r = requests.get("https://api.binance.com/api/v3/ping", timeout=10)
        if r.status_code == 200:
            checks.append(("HTTP Binance /ping", "OK", "200"))
        else:
            checks.append(("HTTP Binance /ping", "FAIL", f"status={r.status_code}"))
    except Exception as exc:
        checks.append(("HTTP Binance /ping", "FAIL", str(exc)))

    try:
        url = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2021-01.zip"
        r = requests.head(url, timeout=15, allow_redirects=True)
        if r.status_code in (200, 301, 302):
            checks.append(("HTTP data.binance.vision", "OK", f"status={r.status_code}"))
        else:
            checks.append(("HTTP data.binance.vision", "FAIL", f"status={r.status_code}"))
    except Exception as exc:
        checks.append(("HTTP data.binance.vision", "FAIL", str(exc)))

    status_rows = []
    failures = 0
    for name, status, detail in checks:
        if status != "OK":
            failures += 1
        status_rows.append([name, status, detail])
    print_table(["Chequeo", "Estado", "Detalle"], status_rows)
    if failures:
        print_error(f"Diagnostico completado con {failures} fallo(s). Revisa detalles arriba.")
    else:
        print("Diagnostico completado: todo OK.")


def print_table(headers, rows) -> None:
    str_rows = [[str(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))

    def _line(ch: str = "-") -> str:
        return "+" + "+".join((ch * (w + 2)) for w in widths) + "+"

    print(_line("-"))
    print("| " + " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))) + " |")
    print(_line("="))
    for row in str_rows:
        print("| " + " | ".join(row[i].ljust(widths[i]) for i in range(len(row))) + " |")
    print(_line("-"))


def show_histogram_overview(db_path: str) -> None:
    print("\n=== Resumen de histogramas en DB ===")
    print(f"DB: {db_path}")
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT symbol, interval, COUNT(*) AS rows_count, MIN(open_time) AS min_ts, MAX(open_time) AS max_ts
            FROM klines
            GROUP BY symbol, interval
            ORDER BY symbol ASC, interval ASC
            """
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as exc:
        logger.exception("No se pudo generar resumen de histogramas: %s", exc)
        print(f"Error al leer resumen de DB: {exc}")
        return

    if not rows:
        print("No hay histogramas cargados en esta DB.")
        print("Tip: usa la opción de descarga API/ZIP para poblar datos.")
        return

    table_rows = []
    total = 0
    for symbol, interval, count, min_ts, max_ts in rows:
        total += int(count or 0)
        table_rows.append([
            symbol,
            interval,
            f"{int(count):,}",
            format_ts(min_ts),
            format_ts(max_ts),
        ])
    print_table(["Simbolo", "Intervalo", "Filas", "Desde (UTC)", "Hasta (UTC)"], table_rows)
    print(f"Total series: {len(rows)} | Total filas: {total:,}")


def parse_timestamp(text: str) -> Optional[int]:
    if not text:
        return None
    try:
        value = int(text)
        if value < 1e11:
            return int(value * 1000)
        return value
    except ValueError:
        try:
            dt = datetime.fromisoformat(text)
            return int(dt.timestamp() * 1000)
        except ValueError:
            print("Formato de fecha no válido. Usa YYYY-MM-DD o un timestamp en ms/segundos.")
            return None


def get_db_min_max(db_path: str, symbol: str, interval: str) -> Optional[tuple]:
    """Return (min_open_time, max_open_time) for symbol/interval in DB, or None if no rows."""
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT MIN(open_time), MAX(open_time) FROM klines WHERE symbol=? AND interval=?",
            (symbol, interval),
        )
        row = cur.fetchone()
        conn.close()
        if row and (row[0] is not None):
            return int(row[0]), int(row[1])
        return None
    except Exception:
        return None


def ask(prompt: str, default: Optional[str] = None) -> str:
    if default:
        v = input(f"{prompt} [{default}]: ").strip()
        if v.lower() in ("exit", "quit"):
            logger.info("User exited via prompt")
            print("Saliendo...")
            sys.exit(0)
        return v or default
    v = input(f"{prompt}: ").strip()
    if v.lower() in ("exit", "quit"):
        logger.info("User exited via prompt")
        print("Saliendo...")
        sys.exit(0)
    return v


def show_menu() -> None:
    print("\n=== Binance Histogram DB Terminal ===")
    print("1) Descargar datos via API")
    print("2) Descargar datos via ZIP mensual")
    print("3) Exportar datos desde la DB (CSV/JSON)")
    print("4) Cambiar ruta de DB")
    print("5) Diagnostico")
    print("6) Salir")


def download_api(db_path: str) -> None:
    symbol = normalize_symbol(ask("Simbolo (por ejemplo BTCUSDT)"))
    interval = normalize_interval(ask("Intervalo (ej. 1m, 1h)"))
    if not symbol or not interval:
        print_error("Debes indicar un simbolo e intervalo validos.")
        return
    start = ask("Fecha inicio (YYYY-MM-DD o timestamp ms/segundos) — predef ALL (Enter)", "predef ALL")
    end = ask("Fecha fin (YYYY-MM-DD o timestamp ms/segundos) — predef ALL (Enter)", "predef ALL")
    batch = ask("Tamano de lote para inserciones", "5000")
    try:
        batch_size = int(batch)
        if batch_size <= 0:
            raise ValueError()
    except Exception:
        print("Tamano de lote invalido. Debe ser un entero mayor que 0.")
        return
    # allow user to step back
    confirm_step = ask("Presiona Enter para continuar, 'b' para volver al menú", "").lower()
    if confirm_step == "b":
        print("Volviendo al menú principal...")
        return
    # For API download, ALL means no local-range filter.
    if start == "predef ALL":
        start_ts = None
    else:
        start_ts = parse_timestamp(start) if start else None
        if start and start_ts is None:
            print_error(f"Fecha inicio inválida: '{start}'. Usa YYYY-MM-DD o timestamp en segundos/ms.")
            return

    if end == "predef ALL":
        end_ts = None
    else:
        end_ts = parse_timestamp(end) if end else None
        if end and end_ts is None:
            print_error(f"Fecha fin inválida: '{end}'. Usa YYYY-MM-DD o timestamp en segundos/ms.")
            return
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        print_error("El rango es inválido: la fecha inicio es mayor que la fecha fin.")
        return
    # estimate total rows from interval and time range when possible
    interval_map = {
        "1m": 60_000,
        "3m": 3 * 60_000,
        "5m": 5 * 60_000,
        "15m": 15 * 60_000,
        "30m": 30 * 60_000,
        "1h": 60 * 60_000,
        "2h": 2 * 60 * 60_000,
        "4h": 4 * 60 * 60_000,
        "6h": 6 * 60 * 60_000,
        "8h": 8 * 60 * 60_000,
        "12h": 12 * 60 * 60_000,
        "1d": 24 * 60 * 60_000,
    }
    estimated_total = None
    if start_ts and end_ts and interval in interval_map:
        estimated_total = max(0, int((end_ts - start_ts) / interval_map[interval]) + 1)

    # summary and confirmation
    print("\n--- Resumen de descarga (API) ---")
    print(f"Simbolo: {symbol}")
    print(f"Intervalo: {interval}")
    print(f"Rango: {start or 'sin inicio especificado'} -> {end or 'sin fin especificado'}")
    print(f"Tamano de lote: {batch_size}")
    if estimated_total is not None:
        print(f"Estimado de filas a descargar: {estimated_total}")
    else:
        print("Estimado de filas: desconocido (rango no especificado o intervalo no mapeado)")
    proceed = ask("Confirmar descarga? (y=si / b=volver / cualquier otra tecla = cancelar)", "y").lower()
    if proceed == "b":
        print("Volviendo al menú principal...")
        logger.info("User backed out before API download: symbol=%s interval=%s", symbol, interval)
        return
    if proceed != "y":
        logger.info("User canceled API download: symbol=%s interval=%s", symbol, interval)
        print("Descarga cancelada.")
        return
    init_db(db_path)
    downloader = BinanceDownloader()
    logger.info("Starting API download: db=%s symbol=%s interval=%s start=%s end=%s batch=%s estimated=%s", db_path, symbol, interval, start_ts, end_ts, batch_size, estimated_total)
    fetched_rows = 0
    batch_list = []
    start_time = time.time()
    before_count = get_series_row_count(db_path, symbol, interval)
    try:
        rows = downloader.download_klines_api(symbol, interval, start_ts=start_ts, end_ts=end_ts)
        # use tqdm with total if estimated_total available
        with tqdm(total=estimated_total, unit="rows", desc="Descargando", leave=True) as pbar:
            for row in rows:
                fetched_rows += 1
                batch_list.append(row)
                if len(batch_list) >= batch_size:
                    insert_klines(db_path, symbol, interval, batch_list)
                    pbar.update(len(batch_list))
                    batch_list = []
            if batch_list:
                insert_klines(db_path, symbol, interval, batch_list)
                pbar.update(len(batch_list))
    except Exception as exc:
        msg = explain_download_error(exc, "Descarga API")
        logger.exception(msg)
        print_error(msg)
        return
    end_time = time.time()
    after_count = get_series_row_count(db_path, symbol, interval)
    new_rows = max(0, after_count - before_count)
    # final summary
    duration = end_time - start_time
    print("\n--- Resumen final de la descarga ---")
    print(f"Simbolo: {symbol}")
    print(f"Intervalo: {interval}")
    print(f"Filas recibidas de Binance: {fetched_rows:,}")
    print(f"Filas nuevas insertadas: {new_rows:,}")
    if fetched_rows == 0:
        print_error(
            "Binance no devolvió datos para ese símbolo/intervalo/rango. "
            "Posibles causas: rango fuera de historial, símbolo inválido o mercado sin datos en ese periodo."
        )
    elif new_rows == 0:
        print("Aviso: se descargaron filas, pero todas ya existían en la DB (duplicados ignorados).")
    if estimated_total is not None:
        print(f"Estimado inicial: {estimated_total}")
    print(f"Tiempo transcurrido: {duration:.2f}s")
    print("Recuerda respetar las políticas de uso justo de la API (límite de peticiones). Se respetaron retardos automáticos en caso de 429.")
    logger.info(
        "API download finished: db=%s symbol=%s interval=%s fetched=%d inserted_new=%d duration=%.2f estimated=%s",
        db_path,
        symbol,
        interval,
        fetched_rows,
        new_rows,
        duration,
        estimated_total,
    )


def download_zip(db_path: str) -> None:
    symbol = normalize_symbol(ask("Simbolo (por ejemplo BTCUSDT)"))
    interval = normalize_interval(ask("Intervalo (ej. 1m, 1h)"))
    if not symbol or not interval:
        print_error("Debes indicar un simbolo e intervalo validos.")
        return
    year_str = ask("Ano (ej. 2024)")
    month_str = ask("Mes (1-12) o 'all' para todo el año", "all")
    try:
        year = int(year_str)
    except Exception:
        print("Año inválido. Cancelando.")
        logger.info("Invalid year input for zip download: %s", year_str)
        return
    batch = ask("Tamano de lote para inserciones", "5000")
    try:
        batch_size = int(batch)
        if batch_size <= 0:
            raise ValueError()
    except Exception:
        print("Tamano de lote invalido. Debe ser un entero mayor que 0.")
        return
    confirm_step = ask("Presiona Enter para continuar, 'b' para volver al menú", "").lower()
    if confirm_step == "b":
        print("Volviendo al menú principal...")
        return

    # validate months before summary
    if month_str.lower() == "all":
        months = list(range(1, 13))
        period_summary = f"{year}-all"
    else:
        try:
            m = int(month_str)
            if not (1 <= m <= 12):
                raise ValueError()
            months = [m]
            period_summary = f"{year}-{m:02d}"
        except Exception:
            print("Mes inválido. Usa 1-12 o 'all'.")
            logger.info("Invalid month input for zip download: %s", month_str)
            return

    # summary and confirmation
    print("\n--- Resumen de descarga (ZIP mensual) ---")
    print(f"Simbolo: {symbol}")
    print(f"Intervalo: {interval}")
    print(f"Periodo: {period_summary}")
    proceed = ask("Confirmar descarga? (y=si / b=volver / cualquier otra tecla = cancelar)", "y").lower()
    if proceed == "b":
        print("Volviendo al menú principal...")
        logger.info("User backed out before ZIP download: symbol=%s interval=%s period=%s batch=%s", symbol, interval, period_summary, batch_size)
        return
    if proceed != "y":
        logger.info("User canceled ZIP download: symbol=%s interval=%s period=%s batch=%s", symbol, interval, period_summary, batch_size)
        print("Descarga cancelada.")
        return
    init_db(db_path)
    downloader = BinanceDownloader()
    logger.info("Starting ZIP download: db=%s symbol=%s interval=%s period=%s batch=%s", db_path, symbol, interval, period_summary, batch_size)
    fetched_rows = 0
    start_time = time.time()
    before_count = get_series_row_count(db_path, symbol, interval)
    months_no_data: List[str] = []
    months_failed: List[str] = []
    try:
        # iterate months and import each
        for m in months:
            month_label = f"{year}-{m:02d}"
            batch_list = []
            any_row = False
            try:
                logger.info("Downloading ZIP for %s %s %02d", symbol, year, m)
                rows_gen = downloader.download_klines_zip(symbol, interval, year, m)
                # Stream rows to avoid building large lists in memory. We don't know total upfront.
                with tqdm(unit="rows", desc=f"Importando {month_label}", leave=True) as pbar:
                    for row in rows_gen:
                        any_row = True
                        fetched_rows += 1
                        batch_list.append(row)
                        if len(batch_list) >= batch_size:
                            insert_klines(db_path, symbol, interval, batch_list)
                            pbar.update(len(batch_list))
                            batch_list = []
                        else:
                            pbar.update(1)
                    if batch_list:
                        insert_klines(db_path, symbol, interval, batch_list)
                        pbar.update(len(batch_list))
                        batch_list = []
                if not any_row:
                    months_no_data.append(month_label)
                    print(f"Aviso: ZIP sin filas para {month_label}.")
                # polite pause between monthly downloads to respect remote service
                time.sleep(0.5)
            except Exception as exc:
                msg = explain_download_error(exc, f"ZIP {month_label}")
                months_failed.append(f"{month_label}: {msg}")
                logger.exception(msg)
                print_error(msg)
                continue
    except Exception as exc:
        msg = explain_download_error(exc, "Descarga ZIP")
        logger.exception(msg)
        print_error(msg)
        return
    end_time = time.time()
    after_count = get_series_row_count(db_path, symbol, interval)
    new_rows = max(0, after_count - before_count)
    duration = end_time - start_time
    print("\n--- Resumen final de la descarga ---")
    print(f"Simbolo: {symbol}")
    print(f"Intervalo: {interval}")
    print(f"Periodo: {period_summary}")
    print(f"Filas leidas desde ZIP: {fetched_rows:,}")
    print(f"Filas nuevas insertadas: {new_rows:,}")
    if months_no_data:
        print(f"Meses sin datos: {', '.join(months_no_data)}")
    if months_failed:
        print("Meses con error:")
        for item in months_failed:
            print(f" - {item}")
    if fetched_rows == 0 and not months_failed:
        print_error(
            "No se obtuvo ninguna fila de los ZIP solicitados. "
            "Posibles causas: símbolo/intervalo incorrecto o periodo sin archivos publicados."
        )
    elif fetched_rows > 0 and new_rows == 0:
        print("Aviso: se leyeron filas del ZIP, pero todas ya existían en la DB (duplicados ignorados).")
    print(f"Tiempo transcurrido: {duration:.2f}s")
    print("Recuerda respetar las políticas de uso justo de la API y del servicio de datos.")
    logger.info("ZIP download finished: db=%s symbol=%s interval=%s period=%s fetched=%d inserted_new=%d duration=%.2f", db_path, symbol, interval, period_summary, fetched_rows, new_rows, duration)


def export_data(db_path: str) -> None:
    import csv
    import json

    symbol = normalize_symbol(ask("Simbolo (por ejemplo BTCUSDT)"))
    interval = normalize_interval(ask("Intervalo (ej. 1m, 1h)"))
    start = ask("Fecha inicio (YYYY-MM-DD o timestamp ms/segundos) — predef ALL (Enter)", "predef ALL")
    end = ask("Fecha fin (YYYY-MM-DD o timestamp ms/segundos) — predef ALL (Enter)", "predef ALL")
    fmt = ask("Formato de export (csv/json)", "csv").lower().strip()
    if fmt not in ("csv", "json"):
        print("Formato no soportado. Usa csv o json.")
        return
    out = ask("Ruta fichero salida", f"{symbol}_{interval}.{'csv' if fmt=='csv' else 'json'}")

    # resolve start/end same as query
    if start == "predef ALL":
        rng = get_db_min_max(db_path, symbol, interval)
        start_ts = rng[0] if rng else None
    else:
        start_ts = parse_timestamp(start) if start else None
        if start and start_ts is None:
            print_error(f"Fecha inicio inválida: '{start}'. Usa YYYY-MM-DD o timestamp en segundos/ms.")
            return
    if end == "predef ALL":
        rng = get_db_min_max(db_path, symbol, interval)
        end_ts = rng[1] if rng else None
    else:
        end_ts = parse_timestamp(end) if end else None
        if end and end_ts is None:
            print_error(f"Fecha fin inválida: '{end}'. Usa YYYY-MM-DD o timestamp en segundos/ms.")
            return
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        print_error("El rango es inválido: la fecha inicio es mayor que la fecha fin.")
        return

    try:
        rows = query_klines(db_path, symbol, interval, start_ts=start_ts, end_ts=end_ts, limit=None)
        if not rows:
            print("No hay filas para exportar.")
            logger.info("Export attempted but no rows: db=%s symbol=%s interval=%s", db_path, symbol, interval)
            return
        if fmt == "csv":
            with open(out, "w", newline='', encoding='utf-8') as fh:
                writer = csv.writer(fh)
                # header
                writer.writerow(["symbol","interval","open_time","open","high","low","close","volume","close_time","quote_asset_volume","num_trades","taker_buy_base","taker_buy_quote","ignore_field"])
                for r in rows:
                    writer.writerow(r)
        else:
            # json
            objs = []
            keys = ["symbol","interval","open_time","open","high","low","close","volume","close_time","quote_asset_volume","num_trades","taker_buy_base","taker_buy_quote","ignore_field"]
            for r in rows:
                objs.append({k: v for k, v in zip(keys, r)})
            with open(out, "w", encoding='utf-8') as fh:
                json.dump(objs, fh, ensure_ascii=False, indent=2)
        print(f"Export completado: {out} ({len(rows)} filas)")
        logger.info("Export completed: %s rows=%d format=%s", out, len(rows), fmt)
    except Exception as exc:
        logger.exception("Error exporting data: %s", exc)
        print(f"Error exportando datos: {exc}")


def main() -> None:
    db_path = DB_PATH
    init_db(db_path)
    print("\nBienvenido a Binance Histogram DB Terminal")
    while True:
        # Keep summary fresh before every new action selection.
        show_histogram_overview(db_path)
        show_menu()
        choice = ask("Selecciona una opción")
        logger.info("Menu selection: %s", choice)
        if choice == "1":
            download_api(db_path)
        elif choice == "2":
            download_zip(db_path)
        elif choice == "3":
            export_data(db_path)
        elif choice == "4":
            db_path = ask("Nueva ruta de DB", db_path)
            init_db(db_path)
            logger.info("DB path changed to: %s", db_path)
            print(f"DB actual cambiada a: {db_path}")
        elif choice == "5":
            run_diagnostics(db_path)
        elif choice == "6":
            print("Saliendo...")
            logger.info("Exiting terminal UI")
            break
        else:
            print("Opción no válida. Intenta de nuevo.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupción detectada. Saliendo.")
        sys.exit(0)
