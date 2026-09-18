"""Local, auditable EUR/USD datasets. No network call happens on import.

Persisted CSV files use UTC bar-open timestamps and midpoint OHLC. Execution
quotes remain separate. Estimated quotes are explicitly identified in metadata.
The public Dukascopy minute endpoint is distinct from its paid S3 tick archive.
"""
from __future__ import annotations

import calendar
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import lzma
import math
from pathlib import Path
import random
import re
import struct
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
UTC = timezone.utc
PIP = 0.0001
FIELDS = ["time", "open", "high", "low", "close"] + [f"{s}_{f}" for s in ("bid", "ask") for f in ("open", "high", "low", "close")]
SOURCE_URL = "https://www.dukascopy.com/swiss/english/marketwatch/historical/"
SYMBOLS = ("EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF")


def _symbol(value: str) -> str:
    symbol = str(value).upper().replace("/", "").strip()
    if symbol not in SYMBOLS:
        raise ValueError("Par admitido: " + ", ".join(SYMBOLS))
    return symbol


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: str) -> tuple[datetime, bool]:
    value = str(value).strip()
    if not value:
        raise ValueError("Falta la fecha/hora de una vela.")
    if re.fullmatch(r"\d{10}(?:\.\d+)?|\d{13}", value):
        number = float(value)
        return datetime.fromtimestamp(number / (1000 if number > 100000000000 else 1), UTC), False
    for fmt in ("%d.%m.%Y %H:%M:%S.%f", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC), True
        except ValueError:
            pass
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Fecha no reconocida: {value!r}. Usa ISO 8601 o DD.MM.YYYY HH:MM:SS (UTC).") from exc
    naive = result.tzinfo is None
    return result.replace(tzinfo=UTC) if naive else result.astimezone(UTC), naive


def _validate_ohlc(values: dict, prefix: str = "") -> None:
    o, h, l, c = (float(values[prefix + f]) for f in ("open", "high", "low", "close"))
    if not all(math.isfinite(p) and p > 0 for p in (o, h, l, c)):
        raise ValueError("Los precios deben ser finitos y mayores que cero.")
    if h < max(o, c, l) or l > min(o, c, h):
        raise ValueError("OHLC inválido: high debe cubrir open/close y low debe quedar debajo.")


def _canonical(time_value: str, bid: dict, ask: dict) -> dict:
    result = {"time": time_value}
    for key in ("open", "high", "low", "close"):
        result["bid_" + key] = round(float(bid[key]), 8)
        result["ask_" + key] = round(float(ask[key]), 8)
        if result["ask_" + key] < result["bid_" + key] - 1e-10:
            raise ValueError("Una cotización ASK es menor que BID.")
        result[key] = round((result["bid_" + key] + result["ask_" + key]) / 2, 8)
    _validate_ohlc(result, "bid_")
    _validate_ohlc(result, "ask_")
    _validate_ohlc(result)
    return result


def _closed_market(moment: datetime) -> bool:
    """Modern New York FX weekend rule, explicitly not a holiday calendar."""
    year = moment.year
    march_first = date(year, 3, 1)
    november_first = date(year, 11, 1)
    dst_start = date(year, 3, 1 + (6 - march_first.weekday()) % 7 + 7)
    dst_end = date(year, 11, 1 + (6 - november_first.weekday()) % 7)
    close_hour = 21 if dst_start <= moment.date() < dst_end else 22
    return moment.weekday() == 5 or (moment.weekday() == 4 and moment.hour >= close_hour) or (moment.weekday() == 6 and moment.hour < close_hour)


def _quality(bars: list[dict], interval_seconds: int | None = None) -> dict:
    times = [datetime.fromisoformat(b["time"].replace("Z", "+00:00")) for b in bars]
    deltas = [int((b - a).total_seconds()) for a, b in zip(times, times[1:])]
    positive = [d for d in deltas if d > 0]
    interval = interval_seconds or (Counter(positive).most_common(1)[0][0] if positive else 60)
    missing = weekends = irregular = gap_count = 0
    examples = []
    for a, b, delta in zip(times, times[1:], deltas):
        if delta % interval:
            irregular += 1
        if delta <= interval:
            continue
        count = max(0, (delta - 1) // interval)
        gap_count += 1
        missing += count
        # Count gaps by day; avoid iterating through millions of absent minutes.
        cursor = a + timedelta(seconds=interval)
        end = b
        closed = 0
        while cursor < end:
            boundary = min(end, (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))
            n = math.ceil((boundary - cursor).total_seconds() / interval)
            if cursor.weekday() == 5:
                closed += n
            elif cursor.weekday() in (4, 6):
                # There can be only one open/closed transition within this day.
                for j in range(n):
                    closed += int(_closed_market(cursor + timedelta(seconds=j * interval)))
            cursor += timedelta(seconds=n * interval)
        weekends += closed
        unexpected = max(0, count - closed)
        if unexpected and len(examples) < 30:
            examples.append({"after": _iso(a), "before": _iso(b), "missing_bars": count, "unexpected_bars": unexpected})
    return {"interval_seconds": interval, "missing_bars": missing, "weekend_missing_bars": weekends,
            "unexpected_missing_bars": max(0, missing - weekends), "gap_count": gap_count,
            "gap_examples": examples, "irregular_intervals": irregular, "duplicate_rows": 0,
            "sorted_input": True, "invalid_rows": 0, "filled_bars": 0,
            "weekend_rule": "Cierre viernes 17:00 a domingo 17:00 Nueva York (regla DST desde 2007); festivos no clasificados."}


def _timeframe(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _save(bars: list[dict], meta: dict) -> dict:
    if not bars:
        raise ValueError("No hay velas válidas para guardar.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{meta['id']}.csv.gz"
    temp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temp, "wt", encoding="utf-8", newline="", compresslevel=6) as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({key: bar[key] for key in FIELDS} for bar in bars)
    temp.replace(path)
    meta.update({"rows": len(bars), "start": bars[0]["time"], "end": bars[-1]["time"],
                 "symbol": meta.get("symbol", "EURUSD"), "storage": "csv.gz", "canonical_price_type": "mid",
                 "timestamp_basis": "UTC, apertura de la vela", "created_at": _iso(datetime.now(UTC))})
    _atomic_text(DATA_DIR / f"{meta['id']}.json", json.dumps(meta, ensure_ascii=False, indent=2, allow_nan=False))
    return meta


def list_datasets() -> list[dict]:
    if not DATA_DIR.exists():
        return []
    result = []
    for file in DATA_DIR.glob("*.json"):
        try:
            meta = json.loads(file.read_text(encoding="utf-8"))
            if meta.get("id") == file.stem and (DATA_DIR / f"{file.stem}.csv.gz").is_file():
                result.append(meta)
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda m: (bool(m.get("synthetic")), m.get("name", "")))


def load_dataset(dataset_id: str) -> tuple[list[dict], dict]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(dataset_id)):
        raise ValueError("Identificador de datos inválido.")
    path = DATA_DIR / f"{dataset_id}.json"
    if not path.is_file():
        raise ValueError("El conjunto de datos no existe.")
    meta = json.loads(path.read_text(encoding="utf-8"))
    with gzip.open(DATA_DIR / f"{dataset_id}.csv.gz", "rt", encoding="utf-8", newline="") as handle:
        bars = [{k: (v if k == "time" else float(v)) for k, v in row.items()} for row in csv.DictReader(handle)]
    return bars, meta


def import_csv(content: str, name: str, price_type: str = "bid", spread_pips: float = 0.2, symbol: str = "EURUSD") -> dict:
    """Import FX OHLC or joined bid/ask OHLC. Naive timestamps mean UTC."""
    symbol = _symbol(symbol)
    if price_type not in ("bid", "ask", "mid", "bid_ask"):
        raise ValueError("Tipo de precio esperado: bid, ask, mid o bid_ask.")
    spread_pips = float(spread_pips)
    if not math.isfinite(spread_pips) or not 0 <= spread_pips <= 100:
        raise ValueError("El spread debe estar entre 0 y 100 pips.")
    content = content.lstrip("\ufeff")
    if not content.strip():
        raise ValueError("El CSV está vacío.")
    try:
        dialect = csv.Sniffer().sniff(content[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("El CSV requiere una fila de cabeceras.")
    normalized = {re.sub(r"[^a-z0-9]", "", h.lower()): h for h in reader.fieldnames}
    time_column = next((normalized[c] for c in ("timestamp", "datetime", "gmttime", "utctime", "time", "date") if c in normalized), None)
    if time_column is None:
        raise ValueError("Falta una columna time, timestamp, datetime o GMT time.")
    quote_columns = {}
    for side in ("bid", "ask"):
        for field in ("open", "high", "low", "close"):
            quote_columns[f"{side}_{field}"] = normalized.get(side + field, normalized.get(field + side))
    joined = all(quote_columns.values())
    if any(quote_columns.values()) and not joined:
        # One fully provided side is also a valid OHLC CSV.
        sides = [s for s in ("bid", "ask") if all(quote_columns[f"{s}_{f}"] for f in ("open", "high", "low", "close"))]
        if len(sides) != 1:
            raise ValueError("Las columnas BID/ASK están incompletas; se requiere OHLC completo de cada lado.")
        price_type = sides[0]
        normalized.update({f: quote_columns[f"{price_type}_{f}"] for f in ("open", "high", "low", "close")})
    if not joined and not all(f in normalized for f in ("open", "high", "low", "close")):
        raise ValueError("Faltan las columnas open, high, low, close o BID/ASK OHLC.")
    if price_type == "bid_ask" and not joined:
        raise ValueError("Se eligió BID/ASK pero el CSV no incluye ambos lados.")
    observed = []
    naive_count = 0
    for row_number, row in enumerate(reader, start=2):
        if not any(v and str(v).strip() for v in row.values()):
            continue
        try:
            stamp = row[time_column]
            if "date" in normalized and "time" in normalized and normalized["date"] != normalized["time"]:
                stamp = str(row[normalized["date"]]) + " " + str(row[normalized["time"]])
            dt, naive = _parse_time(stamp)
            if dt.microsecond:
                raise ValueError("Se esperan velas OHLC con apertura en segundos completos, no ticks.")
            naive_count += naive
            if joined:
                bid = {f: float(row[quote_columns["bid_" + f]]) for f in ("open", "high", "low", "close")}
                ask = {f: float(row[quote_columns["ask_" + f]]) for f in ("open", "high", "low", "close")}
            else:
                values = {f: float(row[normalized[f]]) for f in ("open", "high", "low", "close")}
                spread = spread_pips * (0.01 if symbol.endswith("JPY") else PIP)
                bid_offset = 0 if price_type == "bid" else -spread if price_type == "ask" else -spread / 2
                bid = {f: value + bid_offset for f, value in values.items()}
                ask = {f: value + spread for f, value in bid.items()}
            observed.append(_canonical(_iso(dt), bid, ask))
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(f"Fila {row_number}: {exc}") from exc
        if len(observed) > 2_000_000:
            raise ValueError("Importa como máximo 2 millones de velas por archivo.")
    if len(observed) < 2:
        raise ValueError("El CSV debe contener al menos dos velas válidas.")
    sorted_input = all(a["time"] <= b["time"] for a, b in zip(observed, observed[1:]))
    unique = {bar["time"]: bar for bar in observed}
    bars = [unique[t] for t in sorted(unique)]
    if len(bars) < 2:
        raise ValueError("El CSV debe incluir al menos dos horas de apertura distintas.")
    quality = _quality(bars)
    quality.update({"duplicate_rows": len(observed) - len(bars), "sorted_input": sorted_input})
    warnings = []
    if naive_count:
        warnings.append("Las fechas sin zona horaria se interpretaron como UTC. Verifica la zona del proveedor.")
    if not joined:
        warnings.append(f"Sólo hay precios {price_type.upper()}; el otro lado se estima con spread fijo de {spread_pips:g} pips.")
    else:
        warnings.append("El OHLC medio usa el promedio de extremos BID/ASK; no equivale exactamente al OHLC de ticks medios.")
    if quality["duplicate_rows"]:
        warnings.append(f"Se conservaron las últimas filas de {quality['duplicate_rows']} marcas de tiempo duplicadas.")
    if not sorted_input:
        warnings.append("Las velas se ordenaron cronológicamente.")
    if quality["unexpected_missing_bars"]:
        warnings.append("Hay huecos fuera del cierre semanal estimado; pueden incluir festivos. No se rellenaron.")
    fingerprint = hashlib.sha256((content + price_type + str(spread_pips) + symbol).encode("utf-8")).hexdigest()[:12]
    meta = {"id": "csv-" + fingerprint, "name": str(name or "CSV importado")[:150], "source": "CSV importado por el usuario",
            "price_type": "bid_ask" if joined else price_type, "has_bid_ask": joined, "synthetic": False, "symbol": symbol,
            "timeframe": _timeframe(quality["interval_seconds"]), "spread_pips": None if joined else spread_pips,
            "quality": quality, "warnings": warnings}
    return _save(bars, meta)


def create_demo() -> dict:
    """Three deterministic years for exploring the UI, never market evidence."""
    identifier = "demo-sintetica-eurusd-2023-2025-v1"
    if (DATA_DIR / f"{identifier}.json").exists():
        return json.loads((DATA_DIR / f"{identifier}.json").read_text(encoding="utf-8"))
    rng = random.Random(472019)
    current = datetime(2023, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, tzinfo=UTC)
    bars = []
    close = 1.075
    while current < end:
        if not _closed_market(current):
            t = (current - datetime(2023, 1, 1, tzinfo=UTC)).total_seconds() / 86400
            regime = int(t // 85) % 4
            drift = (0.00011, -0.00012, 0.000025, -0.000015)[regime]
            volatility = (0.0009, 0.0011, 0.0006, 0.0014)[regime]
            op = close + rng.gauss(0, 0.00006)
            close = op + drift + rng.gauss(0, volatility) + (1.08 - op) * 0.001
            high = max(op, close) + abs(rng.gauss(0, volatility * 0.55))
            low = min(op, close) - abs(rng.gauss(0, volatility * 0.55))
            mid = {"open": op, "high": high, "low": low, "close": close}
            spread = (0.3 + rng.random() * 0.7) * PIP
            bid = {k: v - spread / 2 for k, v in mid.items()}
            ask = {k: v + spread / 2 for k, v in mid.items()}
            bars.append(_canonical(_iso(current), bid, ask))
        current += timedelta(hours=1)
    return _save(bars, {"id": identifier, "name": "Demo sintética · EUR/USD · 2023–2025", "source": "Generador sintético local (semilla 472019)",
                        "price_type": "bid_ask", "timeframe": "1h", "synthetic": True, "has_bid_ask": False,
                        "quality": _quality(bars, 3600), "warnings": ["DATOS SINTÉTICOS: sirven para conocer el laboratorio; no validan rentabilidad ni reproducen el mercado.",
                        "Las cotizaciones BID/ASK y el spread también son sintéticos."]})


def _decode_minute_bi5(blob: bytes, day: date, symbol: str = "EURUSD") -> dict[str, dict]:
    """Legacy public DAILY candles: seconds, open, close, low, high, volume.

    The 24-byte big-endian format differs from 20-byte tick records. URL month
    is zero-indexed. Reject misaligned times rather than guessing the time base.
    """
    if not blob:
        return {}
    scale = 1000 if _symbol(symbol).endswith("JPY") else 100000
    raw = lzma.decompress(blob)
    if len(raw) % 24:
        raise ValueError("El archivo de velas de Dukascopy no tiene registros de 24 bytes.")
    midnight = datetime.combine(day, datetime.min.time(), UTC)
    result = {}
    for seconds, op, close, low, high, volume in struct.iter_unpack(">5If", raw):
        if seconds >= 86400 or seconds % 60:
            raise ValueError("El timestamp Dukascopy no corresponde a segundos de una vela M1 dentro del día UTC.")
        values = {"open": op / scale, "high": high / scale, "low": low / scale, "close": close / scale}
        _validate_ohlc(values)
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("Volumen inválido en Dukascopy.")
        # Empty candles are provider-generated carry-forwards, not traded quotes.
        if volume == 0:
            continue
        stamp = _iso(midnight + timedelta(seconds=seconds))
        if stamp in result:
            raise ValueError("Dukascopy contiene minutos duplicados.")
        result[stamp] = values
    return result


def _fetch_day_side(day: date, side: str, symbol: str = "EURUSD") -> tuple[dict, str]:
    relative = f"{symbol}/{day.year}/{day.month - 1:02d}/{day.day:02d}/{side}_candles_min_1.bi5"
    url = "https://datafeed.dukascopy.com/datafeed/" + relative
    path = DATA_DIR / "cache" / relative
    if path.is_file():
        return _decode_minute_bi5(path.read_bytes(), day, symbol), "cache"
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "ForexStrategyLab/1.0 (local historical research)"})
            with urlopen(request, timeout=25) as response:
                blob = response.read(2_000_001)
            if len(blob) > 2_000_000:
                raise ValueError("Archivo de minutos inesperadamente grande.")
            decoded = _decode_minute_bi5(blob, day, symbol)
            temp = path.with_suffix(".tmp")
            temp.write_bytes(blob)
            temp.replace(path)
            return decoded, "downloaded"
        except HTTPError as exc:
            if exc.code == 404:
                return {}, "not_found"
            if exc.code == 429:
                # Honor rate limiting: stop job rather than hammering the host.
                raise RuntimeError("Dukascopy limitó las solicitudes (HTTP 429). Reintenta más tarde; los días ya descargados quedan en caché.") from exc
            if exc.code not in (500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f"Dukascopy respondió HTTP {exc.code} para {day} {side}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt == 2:
                raise RuntimeError(f"No se pudo descargar {day} {side}: {exc}") from exc
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("No se pudo descargar el archivo.")


def download_dukascopy(start: str, end: str, progress=None, symbol: str = "EURUSD") -> dict:
    """Download UTC range [start,end), max 366 days, preserving BID and ASK.

    The callback receives a dictionary with done,total,progress,message. Failed
    files are recorded; only days with both quote sides can enter the dataset.
    A throttled/blocked request fails the job, retaining downloaded raw files.
    """
    symbol = _symbol(symbol)
    try:
        first, last = date.fromisoformat(str(start)), date.fromisoformat(str(end))
    except ValueError as exc:
        raise ValueError("Fechas esperadas en formato YYYY-MM-DD.") from exc
    days_count = (last - first).days
    if not 1 <= days_count <= 366:
        raise ValueError("Cada descarga admite entre 1 y 366 días; la fecha final es exclusiva.")
    if first.year < 2007 or last > datetime.now(UTC).date():
        raise ValueError("Usa fechas desde 2007; sólo se descargan días ya terminados en UTC.")
    days = [first + timedelta(days=i) for i in range(days_count)]
    bars, unavailable, failed, quote_mismatches = [], [], [], 0
    completed = 0

    def fetch(day):
        if day.weekday() == 5:
            return day, {}, {}, "weekend", "weekend"
        bid, bid_status = _fetch_day_side(day, "BID", symbol)
        ask, ask_status = _fetch_day_side(day, "ASK", symbol)
        return day, bid, ask, bid_status, ask_status

    # Two simultaneous days keep public-provider load modest and improve resume.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fetch, day): day for day in days}
        for future in as_completed(futures):
            day = futures[future]
            try:
                _, bid, ask, bs, ass = future.result()
            except RuntimeError as exc:
                if "429" in str(exc) or "10013" in str(exc) or "403" in str(exc):
                    for task in futures:
                        task.cancel()
                    raise
                failed.append({"date": day.isoformat(), "error": str(exc)})
                bid, ask, bs, ass = {}, {}, "failed", "failed"
            except (ValueError, lzma.LZMAError) as exc:
                failed.append({"date": day.isoformat(), "error": str(exc)})
                bid, ask, bs, ass = {}, {}, "invalid", "invalid"
            shared = sorted(bid.keys() & ask.keys())
            quote_mismatches += len(bid.keys() ^ ask.keys())
            if not shared:
                unavailable.append({"date": day.isoformat(), "bid": bs, "ask": ass, "expected_weekend": day.weekday() == 5})
            for stamp in shared:
                try:
                    bars.append(_canonical(stamp, bid[stamp], ask[stamp]))
                except ValueError as exc:
                    failed.append({"date": day.isoformat(), "time": stamp, "error": str(exc)})
            completed += 1
            if progress:
                progress({"done": completed, "total": days_count, "progress": round(completed / days_count * 95, 1),
                          "message": f"Dukascopy: {completed}/{days_count} días, {len(bars):,} velas válidas"})
    if not bars:
        detail = failed[0]["error"] if failed else "El proveedor no devolvió cotizaciones BID/ASK para el intervalo."
        raise ValueError("No se obtuvieron datos reales. " + detail)
    bars.sort(key=lambda b: b["time"])
    quality = _quality(bars, 60)
    quality.update({"unavailable_days": unavailable, "failed_downloads": failed, "unmatched_quote_minutes": quote_mismatches,
                    "requested_start": first.isoformat(), "requested_end_exclusive": last.isoformat(), "zero_volume_bars": "excluidas"})
    warnings = ["Velas M1 BID/ASK, no ticks. El orden de precios dentro de cada minuto es desconocido.",
                "Los extremos del OHLC medio son el promedio de extremos BID/ASK y pueden no ocurrir simultáneamente.",
                "Se excluyen velas sin volumen y minutos que no tengan ambos lados; no se rellenan huecos."]
    if quality["unexpected_missing_bars"] or failed or any(not d["expected_weekend"] for d in unavailable):
        warnings.append("El intervalo tiene ausencias fuera del cierre semanal estimado; revisa la calidad antes de interpretar resultados.")
    meta = {"id": f"dukascopy-{symbol.lower()}-m1-{first:%Y%m%d}-{last:%Y%m%d}", "name": f"Dukascopy · {symbol[:3]}/{symbol[3:]} M1 · {first} — {last} (fin exclusivo)", "symbol": symbol,
            "source": "Dukascopy · datafeed público", "source_url": SOURCE_URL,
            "download_url_pattern": f"https://datafeed.dukascopy.com/datafeed/{symbol}/YYYY/MM0/DD/{{BID,ASK}}_candles_min_1.bi5",
            "synthetic": False, "has_bid_ask": True, "price_type": "bid_ask", "timeframe": "1m", "quality": quality, "warnings": warnings}
    result = _save(bars, meta)
    if progress:
        progress({"done": days_count, "total": days_count, "progress": 100, "message": f"Guardadas {len(bars):,} velas reales"})
    return result
