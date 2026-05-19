"""Terminal UI para explorar la DB local y solicitar descargas de datos Binance."""
import sys
from typing import Optional
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
    print("1) Mostrar información de la DB")
    print("2) Consultar registros de klines")
    print("3) Descargar datos vía API")
    print("4) Descargar datos vía ZIP mensual")
    print("5) Exportar datos desde la DB (CSV/JSON)")
    print("6) Cambiar ruta de DB")
    print("7) Salir")


def show_db_info(db_path: str) -> None:
    try:
        rows = query_klines(db_path, "BTCUSDT", "1h", limit=1)
        print(f"Conexión OK: {db_path}")
        print("Ejemplo registrado encontrado" if rows else "No se encontraron entradas para BTCUSDT 1h")
        logger.info("DB info checked: %s, example found: %s", db_path, bool(rows))
    except Exception as exc:
        logger.exception("Error al leer la DB: %s", exc)
        print(f"Error al leer la DB: {exc}")


def query_data(db_path: str) -> None:
    symbol = ask("Símbolo (por ejemplo BTCUSDT)")
    interval = ask("Intervalo (ej. 1m, 1h)")
    start = ask("Fecha inicio (YYYY-MM-DD o timestamp ms/segundos) — predef ALL (Enter)", "predef ALL")
    end = ask("Fecha fin (YYYY-MM-DD o timestamp ms/segundos) — predef ALL (Enter)", "predef ALL")
    limit = ask("Límite de filas", "20")
    # support preset by pressing Enter (default 'predef ALL') to use full available range in DB
    if start == "predef ALL":
        rng = get_db_min_max(db_path, symbol, interval)
        if rng:
            start_ts = rng[0]
            print(f"Inicio ajustado a primer registro en DB: {start_ts}")
        else:
            print("No hay datos locales para 'ALL'.")
            start_ts = None
    else:
        start_ts = parse_timestamp(start) if start else None

    if end == "predef ALL":
        rng = get_db_min_max(db_path, symbol, interval)
        if rng:
            end_ts = rng[1]
            print(f"Fin ajustado a último registro en DB: {end_ts}")
        else:
            print("No hay datos locales para 'ALL'.")
            end_ts = None
    else:
        end_ts = parse_timestamp(end) if end else None
    try:
        rows = query_klines(db_path, symbol, interval, start_ts=start_ts, end_ts=end_ts, limit=int(limit))
        if not rows:
            print("No hay filas coincidentes.")
            logger.info("Query returned no rows: db=%s symbol=%s interval=%s start=%s end=%s", db_path, symbol, interval, start, end)
            return
        for r in rows:
            print(r)
        print(f"Total filas mostradas: {len(rows)}")
        logger.info("Query returned %d rows: db=%s symbol=%s interval=%s", len(rows), db_path, symbol, interval)
    except Exception as exc:
        logger.exception("Error en la consulta: %s", exc)
        print(f"Error en la consulta: {exc}")


def download_api(db_path: str) -> None:
    symbol = ask("Símbolo (por ejemplo BTCUSDT)")
    interval = ask("Intervalo (ej. 1m, 1h)")
    start = ask("Fecha inicio (YYYY-MM-DD o timestamp ms/segundos) — predef ALL (Enter)", "predef ALL")
    end = ask("Fecha fin (YYYY-MM-DD o timestamp ms/segundos) — predef ALL (Enter)", "predef ALL")
    batch = ask("Tamaño de lote para inserciones", "5000")
    # allow user to step back
    confirm_step = ask("Presiona Enter para continuar, 'b' para volver al menú", "").lower()
    if confirm_step == "b":
        print("Volviendo al menú principal...")
        return
    # support 'all' preset: use available range in DB if present
    if start == "predef ALL":
        rng = get_db_min_max(db_path, symbol, interval)
        if rng:
            start_ts = rng[0]
            print(f"Inicio ajustado a primer registro en DB: {start_ts}")
        else:
            start_ts = None
    else:
        start_ts = parse_timestamp(start) if start else None

    if end == "predef ALL":
        rng = get_db_min_max(db_path, symbol, interval)
        if rng:
            end_ts = rng[1]
            print(f"Fin ajustado a último registro en DB: {end_ts}")
        else:
            end_ts = None
    else:
        end_ts = parse_timestamp(end) if end else None
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
    print(f"Símbolo: {symbol}")
    print(f"Intervalo: {interval}")
    print(f"Rango: {start or 'sin inicio especificado'} -> {end or 'sin fin especificado'}")
    print(f"Tamaño de lote: {batch}")
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
    logger.info("Starting API download: db=%s symbol=%s interval=%s start=%s end=%s batch=%s estimated=%s", db_path, symbol, interval, start_ts, end_ts, batch, estimated_total)
    rows = downloader.download_klines_api(symbol, interval, start_ts=start_ts, end_ts=end_ts)
    inserted = 0
    batch_list = []
    start_time = time.time()
    try:
        # use tqdm with total if estimated_total available
        with tqdm(total=estimated_total, unit="rows", desc="Descargando", leave=True) as pbar:
            for row in rows:
                batch_list.append(row)
                if len(batch_list) >= int(batch):
                    insert_klines(db_path, symbol, interval, batch_list)
                    inserted += len(batch_list)
                    pbar.update(len(batch_list))
                    batch_list = []
            if batch_list:
                insert_klines(db_path, symbol, interval, batch_list)
                inserted += len(batch_list)
                pbar.update(len(batch_list))
    except Exception as exc:
        logger.exception("Error de descarga/inserción: %s", exc)
        print(f"Error de descarga/inserción: {exc}")
    end_time = time.time()
    # final summary
    duration = end_time - start_time
    print("\n--- Resumen final de la descarga ---")
    print(f"Símbolo: {symbol}")
    print(f"Intervalo: {interval}")
    print(f"Filas insertadas: {inserted}")
    if estimated_total is not None:
        print(f"Estimado inicial: {estimated_total}")
    print(f"Tiempo transcurrido: {duration:.2f}s")
    print("Recuerda respetar las políticas de uso justo de la API (límite de peticiones). Se respetaron retardos automáticos en caso de 429.")
    logger.info("API download finished: db=%s symbol=%s interval=%s inserted=%d duration=%.2f estimated=%s", db_path, symbol, interval, inserted, duration, estimated_total)


def download_zip(db_path: str) -> None:
    symbol = ask("Símbolo (por ejemplo BTCUSDT)")
    interval = ask("Intervalo (ej. 1m, 1h)")
    year_str = ask("Año (ej. 2024)")
    month_str = ask("Mes (1-12) o 'all' para todo el año", "all")
    try:
        year = int(year_str)
    except Exception:
        print("Año inválido. Cancelando.")
        logger.info("Invalid year input for zip download: %s", year_str)
        return
    batch = ask("Tamaño de lote para inserciones", "5000")
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
    print(f"Símbolo: {symbol}")
    print(f"Intervalo: {interval}")
    print(f"Periodo: {period_summary}")
    proceed = ask("Confirmar descarga? (y=si / b=volver / cualquier otra tecla = cancelar)", "y").lower()
    if proceed == "b":
        print("Volviendo al menú principal...")
        logger.info("User backed out before ZIP download: symbol=%s interval=%s period=%s batch=%s", symbol, interval, period_summary, batch)
        return
    if proceed != "y":
        logger.info("User canceled ZIP download: symbol=%s interval=%s period=%s batch=%s", symbol, interval, period_summary, batch)
        print("Descarga cancelada.")
        return
    init_db(db_path)
    downloader = BinanceDownloader()
    logger.info("Starting ZIP download: db=%s symbol=%s interval=%s period=%s batch=%s", db_path, symbol, interval, period_summary, batch)
    inserted = 0
    batch_list = []
    start_time = time.time()
    try:
        # iterate months and import each
        total_inserted = 0
        for m in months:
            logger.info("Downloading ZIP for %s %s %02d", symbol, year, m)
            rows_gen = downloader.download_klines_zip(symbol, interval, year, m)
            # Stream rows to avoid building large lists in memory. We don't know total upfront.
            any_row = False
            with tqdm(unit="rows", desc=f"Importando {year}-{m:02d}", leave=True) as pbar:
                for row in rows_gen:
                    any_row = True
                    batch_list.append(row)
                    if len(batch_list) >= int(batch):
                        insert_klines(db_path, symbol, interval, batch_list)
                        inserted += len(batch_list)
                        total_inserted += len(batch_list)
                        pbar.update(len(batch_list))
                        batch_list = []
                    else:
                        pbar.update(1)
                if batch_list:
                    insert_klines(db_path, symbol, interval, batch_list)
                    inserted += len(batch_list)
                    total_inserted += len(batch_list)
                    pbar.update(len(batch_list))
                    batch_list = []
            if not any_row:
                print(f"No hay datos en el ZIP para {year}-{m:02d}")
            # polite pause between monthly downloads to respect remote service
            time.sleep(0.5)
        inserted = total_inserted
    except Exception as exc:
        logger.exception("Error de descarga/inserción (zip): %s", exc)
        print(f"Error de descarga/inserción: {exc}")
    end_time = time.time()
    duration = end_time - start_time
    print("\n--- Resumen final de la descarga ---")
    print(f"Símbolo: {symbol}")
    print(f"Intervalo: {interval}")
    print(f"Periodo: {period_summary}")
    print(f"Filas insertadas: {inserted}")
    print(f"Tiempo transcurrido: {duration:.2f}s")
    print("Recuerda respetar las políticas de uso justo de la API y del servicio de datos.")
    logger.info("ZIP download finished: db=%s symbol=%s interval=%s period=%s inserted=%d duration=%.2f", db_path, symbol, interval, period_summary, inserted, duration)


def export_data(db_path: str) -> None:
    import csv
    import json

    symbol = ask("Símbolo (por ejemplo BTCUSDT)")
    interval = ask("Intervalo (ej. 1m, 1h)")
    start = ask("Fecha inicio (YYYY-MM-DD o timestamp ms/segundos) — predef ALL (Enter)", "predef ALL")
    end = ask("Fecha fin (YYYY-MM-DD o timestamp ms/segundos) — predef ALL (Enter)", "predef ALL")
    fmt = ask("Formato de export (csv/json)", "csv").lower()
    out = ask("Ruta fichero salida", f"{symbol}_{interval}.{'csv' if fmt=='csv' else 'json'}")

    # resolve start/end same as query
    if start == "predef ALL":
        rng = get_db_min_max(db_path, symbol, interval)
        start_ts = rng[0] if rng else None
    else:
        start_ts = parse_timestamp(start) if start else None
    if end == "predef ALL":
        rng = get_db_min_max(db_path, symbol, interval)
        end_ts = rng[1] if rng else None
    else:
        end_ts = parse_timestamp(end) if end else None

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
    while True:
        show_menu()
        choice = ask("Selecciona una opción")
        logger.info("Menu selection: %s", choice)
        if choice == "1":
            show_db_info(db_path)
        elif choice == "2":
            query_data(db_path)
        elif choice == "3":
            download_api(db_path)
        elif choice == "4":
            download_zip(db_path)
        elif choice == "5":
            export_data(db_path)
        elif choice == "6":
            db_path = ask("Nueva ruta de DB", db_path)
            init_db(db_path)
            logger.info("DB path changed to: %s", db_path)
            print(f"DB actual cambiada a: {db_path}")
        elif choice == "7":
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
