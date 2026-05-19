"""Terminal UI para explorar la DB local y solicitar descargas de datos Binance."""
import sys
from typing import Optional
from db import init_db, query_klines, insert_klines
from binance_hist_downloader import BinanceDownloader
from datetime import datetime
import time
from tqdm import tqdm
import logging
import logging.handlers
import os

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


def ask(prompt: str, default: Optional[str] = None) -> str:
    if default:
        return input(f"{prompt} [{default}]: ").strip() or default
    return input(f"{prompt}: ").strip()


def show_menu() -> None:
    print("\n=== Binance Histogram DB Terminal ===")
    print("1) Mostrar información de la DB")
    print("2) Consultar registros de klines")
    print("3) Descargar datos vía API")
    print("4) Descargar datos vía ZIP mensual")
    print("5) Cambiar ruta de DB")
    print("6) Salir")


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
    start = ask("Fecha inicio (YYYY-MM-DD o timestamp ms/segundos) o vacío", "")
    end = ask("Fecha fin (YYYY-MM-DD o timestamp ms/segundos) o vacío", "")
    limit = ask("Límite de filas", "20")
    start_ts = parse_timestamp(start) if start else None
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
    start = ask("Fecha inicio (YYYY-MM-DD o timestamp ms/segundos) o vacío", "")
    end = ask("Fecha fin (YYYY-MM-DD o timestamp ms/segundos) o vacío", "")
    batch = ask("Tamaño de lote para inserciones", "5000")
    # allow user to step back
    confirm_step = ask("Presiona Enter para continuar, 'b' para volver al menú", "").lower()
    if confirm_step == "b":
        print("Volviendo al menú principal...")
        return
    start_ts = parse_timestamp(start) if start else None
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
    year = int(ask("Año (ej. 2024)"))
    month = int(ask("Mes (1-12)"))
    batch = ask("Tamaño de lote para inserciones", "5000")
    confirm_step = ask("Presiona Enter para continuar, 'b' para volver al menú", "").lower()
    if confirm_step == "b":
        print("Volviendo al menú principal...")
        return
    # summary and confirmation
    print("\n--- Resumen de descarga (ZIP mensual) ---")
    print(f"Símbolo: {symbol}")
    print(f"Intervalo: {interval}")
    print(f"Periodo: {year}-{month:02d}")
    proceed = ask("Confirmar descarga? (y=si / b=volver / cualquier otra tecla = cancelar)", "y").lower()
    if proceed == "b":
        print("Volviendo al menú principal...")
        logger.info("User backed out before ZIP download: symbol=%s interval=%s period=%s-%s", symbol, interval, year, month)
        return
    if proceed != "y":
        logger.info("User canceled ZIP download: symbol=%s interval=%s period=%s-%s", symbol, interval, year, month)
        print("Descarga cancelada.")
        return
    init_db(db_path)
    downloader = BinanceDownloader()
    logger.info("Starting ZIP download: db=%s symbol=%s interval=%s period=%s-%s batch=%s", db_path, symbol, interval, year, month, batch)
    rows = downloader.download_klines_zip(symbol, interval, year, month)
    inserted = 0
    batch_list = []
    start_time = time.time()
    try:
        # For zip we can attempt to count rows first to provide a proper progress bar
        rows_list = list(rows)
        total = len(rows_list)
        with tqdm(total=total, unit="rows", desc="Importando zip", leave=True) as pbar:
            for row in rows_list:
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
        logger.exception("Error de descarga/inserción (zip): %s", exc)
        print(f"Error de descarga/inserción: {exc}")
    end_time = time.time()
    duration = end_time - start_time
    print("\n--- Resumen final de la descarga ---")
    print(f"Símbolo: {symbol}")
    print(f"Intervalo: {interval}")
    print(f"Periodo: {year}-{month:02d}")
    print(f"Filas insertadas: {inserted}")
    print(f"Tiempo transcurrido: {duration:.2f}s")
    print("Recuerda respetar las políticas de uso justo de la API y del servicio de datos.")
    logger.info("ZIP download finished: db=%s symbol=%s interval=%s period=%s-%s inserted=%d duration=%.2f", db_path, symbol, interval, year, month, inserted, duration)


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
            db_path = ask("Nueva ruta de DB", db_path)
            init_db(db_path)
            logger.info("DB path changed to: %s", db_path)
            print(f"DB actual cambiada a: {db_path}")
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
