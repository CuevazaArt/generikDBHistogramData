"""Terminal UI para explorar la DB local y solicitar descargas de datos Binance."""
import sys
from typing import Optional
from db import init_db, query_klines, insert_klines
from binance_hist_downloader import BinanceDownloader
from datetime import datetime

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
    except Exception as exc:
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
            return
        for r in rows:
            print(r)
        print(f"Total filas mostradas: {len(rows)}")
    except Exception as exc:
        print(f"Error en la consulta: {exc}")


def download_api(db_path: str) -> None:
    symbol = ask("Símbolo (por ejemplo BTCUSDT)")
    interval = ask("Intervalo (ej. 1m, 1h)")
    start = ask("Fecha inicio (YYYY-MM-DD o timestamp ms/segundos) o vacío", "")
    end = ask("Fecha fin (YYYY-MM-DD o timestamp ms/segundos) o vacío", "")
    batch = ask("Tamaño de lote para inserciones", "5000")
    start_ts = parse_timestamp(start) if start else None
    end_ts = parse_timestamp(end) if end else None
    init_db(db_path)
    downloader = BinanceDownloader()
    rows = downloader.download_klines_api(symbol, interval, start_ts=start_ts, end_ts=end_ts)
    inserted = 0
    batch_list = []
    try:
        for row in rows:
            batch_list.append(row)
            if len(batch_list) >= int(batch):
                insert_klines(db_path, symbol, interval, batch_list)
                inserted += len(batch_list)
                batch_list = []
        if batch_list:
            insert_klines(db_path, symbol, interval, batch_list)
            inserted += len(batch_list)
        print(f"Inserción completada. Filas agregadas: {inserted}")
    except Exception as exc:
        print(f"Error de descarga/inserción: {exc}")


def download_zip(db_path: str) -> None:
    symbol = ask("Símbolo (por ejemplo BTCUSDT)")
    interval = ask("Intervalo (ej. 1m, 1h)")
    year = int(ask("Año (ej. 2024)"))
    month = int(ask("Mes (1-12)"))
    batch = ask("Tamaño de lote para inserciones", "5000")
    init_db(db_path)
    downloader = BinanceDownloader()
    rows = downloader.download_klines_zip(symbol, interval, year, month)
    inserted = 0
    batch_list = []
    try:
        for row in rows:
            batch_list.append(row)
            if len(batch_list) >= int(batch):
                insert_klines(db_path, symbol, interval, batch_list)
                inserted += len(batch_list)
                batch_list = []
        if batch_list:
            insert_klines(db_path, symbol, interval, batch_list)
            inserted += len(batch_list)
        print(f"Inserción completada. Filas agregadas: {inserted}")
    except Exception as exc:
        print(f"Error de descarga/inserción: {exc}")


def main() -> None:
    db_path = DB_PATH
    init_db(db_path)
    while True:
        show_menu()
        choice = ask("Selecciona una opción")
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
            print(f"DB actual cambiada a: {db_path}")
        elif choice == "6":
            print("Saliendo...")
            break
        else:
            print("Opción no válida. Intenta de nuevo.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupción detectada. Saliendo.")
        sys.exit(0)
