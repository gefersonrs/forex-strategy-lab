"""Local-only HTTP application. Run with python server.py; no installed web framework needed."""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from backend import data, engine

STATE = ROOT / 'data' / 'experiments'
STATE.mkdir(parents=True, exist_ok=True)
RUNS = ROOT / 'data' / 'runs'
RUNS.mkdir(parents=True, exist_ok=True)
RESULTS = {}
JOBS = {}
HOLDOUTS = {}
LOCK = threading.RLock()
DATA_CACHE = {}
APP_PORT = 8765
STARTUP_RESULT = None


def now():
    return datetime.now(timezone.utc).isoformat()


def json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def dataset_bars(dataset_id):
    with LOCK:
        if dataset_id in DATA_CACHE:
            return DATA_CACHE[dataset_id]
    loaded = data.load_dataset(dataset_id)
    with LOCK:
        DATA_CACHE.clear()  # keep memory bounded to one active history
        DATA_CACHE[dataset_id] = loaded
    return loaded


def remember(result):
    result['id'] = uuid.uuid4().hex
    result['created_at'] = now()
    with gzip.open(RUNS / (result['id'] + '.json.gz'), 'wb') as handle:
        handle.write(json_bytes(result))
    with LOCK:
        RESULTS[result['id']] = result
        while len(RESULTS) > 20:
            del RESULTS[next(iter(RESULTS))]
    return result


def run(payload):
    dataset_id = payload.get('dataset_id')
    if not dataset_id:
        raise ValueError('Selecciona un conjunto de datos.')
    bars, metadata = dataset_bars(dataset_id)
    config = {**engine.default_config(), **payload.get('config', {})}
    config['symbol'] = metadata.get('symbol', 'EURUSD').replace('/', '').upper()
    config['observed_bid_ask'] = bool(metadata.get('has_bid_ask', False))
    config['input_price_type'] = metadata.get('price_type', 'mid')
    if config['input_price_type'] not in ('bid', 'ask', 'mid'):
        config['input_price_type'] = 'mid'
    result = engine.run_backtest(bars, config, payload.get('code'))
    result['dataset'] = metadata
    result['code'] = payload.get('code') or getattr(engine, 'DEFAULT_CODE', '')
    return remember(result)


def experiment_list():
    entries = []
    for path in STATE.glob('*.json.gz'):
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as f:
                item = json.load(f)
            entries.append({k: item.get(k) for k in ('id', 'name', 'hypothesis', 'created_at', 'metrics', 'config', 'dataset')})
        except (OSError, ValueError):
            continue
    return sorted(entries, key=lambda x: x.get('created_at', ''), reverse=True)


def valid_id(value):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value):
        raise ValueError('Identificador no válido.')
    return value


def get_result(result_id):
    valid_id(result_id)
    with LOCK:
        result = RESULTS.get(result_id)
    if result is not None:
        return result
    for folder in (STATE, RUNS):
        path = folder / (result_id + '.json.gz')
        if path.exists():
            with gzip.open(path, 'rt', encoding='utf-8') as f:
                return json.load(f)
    raise ValueError('La prueba ya no está en memoria. Ábrela desde Experimentos o ejecútala de nuevo.')


def save_experiment(payload):
    item = dict(get_result(str(payload.get('result_id', ''))))
    item['id'] = uuid.uuid4().hex
    item['name'] = str(payload.get('name') or 'Experimento sin nombre')[:160]
    item['hypothesis'] = str(payload.get('hypothesis') or '')[:5000]
    path = STATE / (item['id'] + '.json.gz')
    temporary = path.with_suffix('.tmp')
    with gzip.open(temporary, 'wb') as f:
        f.write(json_bytes(item))
    temporary.replace(path)
    return {k: item[k] for k in ('id', 'name', 'hypothesis', 'created_at', 'metrics', 'config', 'dataset')}


def download_job(payload):
    start, end = str(payload.get('start', '')), str(payload.get('end', ''))
    # Validate before creating a background job, including a bounded range.
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if not 0 < (finish - begin).days <= 366:
        raise ValueError('Elige entre 1 y 366 días por descarga. La fecha final es exclusiva.')
    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {'job_id': job_id, 'status': 'running', 'progress': 0, 'message': 'Descargando EUR/USD de Dukascopy…'}

    def progress(*args, **kwargs):
        value = args[0] if args else kwargs.get('progress', 0)
        update = value if isinstance(value, dict) else {'progress': value}
        if len(args) > 1 and isinstance(args[1], str):
            update['message'] = args[1]
        with LOCK:
            JOBS[job_id].update(update)

    def work():
        try:
            meta = data.download_dukascopy(start, end, progress=progress, symbol=payload.get('symbol', 'EURUSD'))
            with LOCK:
                JOBS[job_id].update(status='completed', progress=100, message='Histórico guardado.', dataset=meta)
        except Exception as exc:
            with LOCK:
                JOBS[job_id].update(status='failed', error=str(exc), message='No se pudo completar la descarga.')

    threading.Thread(target=work, daemon=True).start()
    return {'job_id': job_id}


def research(payload):
    base = dict(payload)
    config = {**engine.default_config(), **payload.get('config', {})}
    baseline = run({**base, 'config': config})
    stressed = {**config, 'commission_per_lot_side': config['commission_per_lot_side'] * 2,
                'slippage_pips': config['slippage_pips'] * 2, 'spread_pips': config['spread_pips'] * 2}
    stress = run({**base, 'config': stressed})
    null = run({**base, 'config': {**config, 'strategy': 'random'}})
    return {'runs': [
        {'label': 'Configuración actual', 'metrics': baseline['metrics'], 'result_id': baseline['id']},
        {'label': 'Comisión y slippage ×2', 'metrics': stress['metrics'], 'result_id': stress['id']},
        {'label': 'Control aleatorio · misma semilla', 'metrics': null['metrics'], 'result_id': null['id']},
    ], 'note': 'Mismo histórico y capital inicial. El spread bid/ask observado se conserva; el spread supuesto se duplica cuando los datos no incluyen ask. Esta comparación es exploratoria y no es una validación fuera de muestra.'}


def sensitivity(payload):
    atrs = [float(x) for x in payload.get('atr_values', [1.5, 2, 2.5])]
    breaks = [int(x) for x in payload.get('breakout_values', [10, 20, 30])]
    if not 1 <= len(atrs) <= 5 or not 1 <= len(breaks) <= 5:
        raise ValueError('Usa de 1 a 5 valores por parámetro, hasta 25 combinaciones.')
    if any(not 0.1 <= x <= 20 for x in atrs) or any(not 2 <= x <= 500 for x in breaks):
        raise ValueError('ATR entre 0.1 y 20; ruptura entre 2 y 500 velas.')
    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {'job_id': job_id, 'status': 'running', 'progress': 0, 'message': 'Midiendo sensibilidad…'}

    def work():
        try:
            cells = []
            for atr in atrs:
                for breakout in breaks:
                    cfg = {**payload.get('config', {}), 'atr_multiplier': atr, 'breakout_period': breakout}
                    item = run({**payload, 'config': cfg})
                    cells.append({'atr_multiplier': atr, 'breakout_period': breakout, 'metrics': item['metrics']})
                    with LOCK:
                        JOBS[job_id].update(progress=round(len(cells) / (len(atrs) * len(breaks)) * 100),
                                            message=f'{len(cells)} de {len(atrs) * len(breaks)} combinaciones')
            with LOCK:
                JOBS[job_id].update(status='completed', result={'cells': cells, 'note': 'Matriz exploratoria sobre el período seleccionado. Busca estabilidad entre vecinos; elegir el máximo retrospectivo no demuestra una ventaja fuera de muestra.'})
        except Exception as exc:
            with LOCK:
                JOBS[job_id].update(status='failed', error=str(exc))

    threading.Thread(target=work, daemon=True).start()
    return {'job_id': job_id}


def holdout(payload, reveal=False):
    if reveal:
        ticket_id = valid_id(str(payload.get('ticket_id', '')))
        item = HOLDOUTS.get(ticket_id)
        if item is None:
            raise ValueError('Prepara primero una separación temporal. Se conserva hasta que cierres el servidor.')
        if 'test' not in item:
            frozen = item['payload']
            cfg = {**frozen['config'], 'trade_start': item['boundary'], 'force_close': True}
            item['test'] = run({**frozen, 'config': cfg})
        return {key: item[key] for key in ('ticket_id', 'boundary', 'train', 'test', 'note')}
    split = float(payload.get('split', 0.7))
    if not 0.5 <= split <= 0.9:
        raise ValueError('La proporción de desarrollo debe estar entre 50% y 90%.')
    bars, _ = dataset_bars(payload['dataset_id'])
    cfg = {**engine.default_config(), **payload.get('config', {})}
    start, end = cfg.get('trade_start'), cfg.get('trade_end')
    def parse(value):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    start_dt, end_dt = parse(start) if start else None, parse(end) if end else None
    eligible = [b for b in bars if (not start_dt or parse(b['time']) >= start_dt) and (not end_dt or parse(b['time']) < end_dt)]
    if len(eligible) < 100:
        raise ValueError('La separación requiere por lo menos 100 velas de ejecución.')
    boundary = eligible[int(len(eligible) * split)]['time']
    ticket_id = uuid.uuid4().hex
    frozen = {'dataset_id': payload['dataset_id'], 'config': cfg, 'code': payload.get('code')}
    train = run({**frozen, 'config': {**cfg, 'trade_end': boundary, 'force_close': True}})
    item = {'ticket_id': ticket_id, 'boundary': boundary, 'payload': frozen, 'train': train,
            'note': 'Reglas y código congelados al preparar esta prueba. El segmento reservado comienza con capital independiente y conserva el histórico anterior solo para calcular indicadores. Revelarlo repetidamente convierte ese período en datos de desarrollo.'}
    HOLDOUTS[ticket_id] = item
    return {key: item[key] for key in ('ticket_id', 'boundary', 'train', 'note')}


class Handler(BaseHTTPRequestHandler):
    server_version = 'ForexLab/0.1'

    def log_message(self, format, *args):
        if len(args) > 1 and str(args[1]).startswith('5'):
            super().log_message(format, *args)

    def send(self, status, body, content_type='application/json; charset=utf-8'):
        raw = body if isinstance(body, bytes) else json_bytes(body)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self):
        host = self.headers.get('Host', '')
        expected = {f'127.0.0.1:{APP_PORT}', f'localhost:{APP_PORT}'}
        if host not in expected:
            self.send(403, {'error': 'El laboratorio solo admite acceso local.'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin not in {f'http://{h}' for h in expected}:
            self.send(403, {'error': 'Origen no permitido.'})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        path = urlparse(self.path).path
        try:
            if path == '/api/health':
                return self.send(200, {'status': 'ok', 'version': '0.1.0', 'local_only': True})
            if path == '/api/bootstrap':
                return self.send(200, {'config': engine.default_config(), 'code': getattr(engine, 'DEFAULT_CODE', ''),
                    'datasets': data.list_datasets(), 'experiments': experiment_list(), 'last_result': STARTUP_RESULT})
            if path == '/api/datasets':
                return self.send(200, data.list_datasets())
            if path == '/api/experiments':
                return self.send(200, experiment_list())
            if path.startswith('/api/experiments/') or path.startswith('/api/results/'):
                return self.send(200, get_result(path.rsplit('/', 1)[-1]))
            if path.startswith('/api/jobs/'):
                with LOCK:
                    job = dict(JOBS.get(path.rsplit('/', 1)[-1], {}))
                if not job:
                    return self.send(404, {'error': 'Descarga no encontrada.'})
                return self.send(200, job)
            if path.startswith('/static/'):
                path = path.removeprefix('/static')
            files = {'/': ('index.html', 'text/html'), '/index.html': ('index.html', 'text/html'),
                     '/app.js': ('app.js', 'text/javascript'), '/app.css': ('app.css', 'text/css')}
            if path in files:
                filename, content_type = files[path]
                return self.send(200, (ROOT / 'static' / filename).read_bytes(), content_type + '; charset=utf-8')
            return self.send(404, {'error': 'Página no encontrada.'})
        except (ValueError, KeyError, FileNotFoundError) as exc:
            self.send(400, {'error': str(exc)})
        except Exception:
            traceback.print_exc()
            self.send(500, {'error': 'No se pudo leer el resultado. Revisa el registro local.'})

    def do_POST(self):
        if not self.allowed():
            return
        try:
            if not self.headers.get('Content-Type', '').startswith('application/json'):
                return self.send(415, {'error': 'Se requiere JSON.'})
            size = int(self.headers.get('Content-Length', '0'))
            if size < 1 or size > 96 * 1024 * 1024:
                return self.send(413, {'error': 'El archivo debe ser menor de 96 MB. Divide el histórico en intervalos.'})
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('Solicitud no válida.')
            path = urlparse(self.path).path
            if path == '/api/backtest':
                result = run(payload)
            elif path == '/api/monte-carlo':
                count = int(payload.get('simulations', 500))
                if not 100 <= count <= 5000:
                    raise ValueError('Elige entre 100 y 5,000 simulaciones.')
                result = engine.monte_carlo(get_result(str(payload.get('result_id', ''))), count, int(payload.get('seed', 42)))
            elif path == '/api/experiments':
                result = save_experiment(payload)
            elif path == '/api/import':
                result = data.import_csv(str(payload.get('content', '')), str(payload.get('name', 'Histórico CSV')),
                                         str(payload.get('price_type', 'bid')), float(payload.get('spread_pips', 0.2)), symbol=payload.get('symbol', 'EURUSD'))
            elif path == '/api/download':
                result = download_job(payload)
            elif path == '/api/research':
                result = research(payload)
            elif path == '/api/sensitivity':
                result = sensitivity(payload)
            elif path == '/api/holdout':
                result = holdout(payload)
            elif path == '/api/holdout/reveal':
                result = holdout(payload, reveal=True)
            elif path == '/api/validate-code':
                engine.validate_code(str(payload.get('code', '')))
                result = {'valid': True}
            else:
                return self.send(404, {'error': 'Acción no encontrada.'})
            self.send(200, result)
        except (ValueError, KeyError, TypeError, SyntaxError) as exc:
            self.send(400, {'error': str(exc)})
        except Exception as exc:
            traceback.print_exc()
            self.send(500, {'error': 'La operación no se completó: ' + str(exc)[:350]})


def main():
    global APP_PORT, STARTUP_RESULT
    parser = argparse.ArgumentParser(description='Forex Strategy Lab · laboratorio local')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-demo-run', action='store_true')
    args = parser.parse_args()
    APP_PORT = args.port
    demo = data.create_demo()
    if not args.no_demo_run:
        STARTUP_RESULT = run({'dataset_id': demo['id'], 'config': engine.default_config()})
    server = ThreadingHTTPServer(('127.0.0.1', APP_PORT), Handler)
    print(f'Forex Strategy Lab listo en http://127.0.0.1:{APP_PORT}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
