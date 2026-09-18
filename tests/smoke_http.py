"""Run against an already started local server; saves two explicitly synthetic examples."""
import json
from urllib.request import Request, urlopen

BASE = 'http://127.0.0.1:8765'

def call(path, payload=None):
    req = Request(BASE + path, data=json.dumps(payload).encode() if payload is not None else None,
                  headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=120) as response:
        return json.load(response)

if __name__ == '__main__':
    boot = call('/api/bootstrap')
    dataset = next(d for d in boot['datasets'] if d['synthetic'])
    ids = []
    for strategy, name in [('trend', 'DEMO · Tendencia · $10,000'), ('random', 'DEMO · Azar · $10,000')]:
        result = call('/api/backtest', {'dataset_id': dataset['id'], 'config': {'initial_balance': 10000, 'strategy': strategy}})
        assert result['metrics']['total_trades'] > 0
        assert result['dataset']['synthetic']
        assert result['config']['initial_balance'] == 10000
        saved = call('/api/experiments', {'result_id': result['id'], 'name': name,
                    'hypothesis': 'Demostración sobre precios sintéticos para explorar los controles; no valida una ventaja histórica.'})
        assert call('/api/experiments/' + saved['id'])['metrics'] == result['metrics']
        mc = call('/api/monte-carlo', {'result_id': result['id'], 'simulations': 100, 'seed': 42})
        assert mc['terminal']['p5'] <= mc['terminal']['p50'] <= mc['terminal']['p95']
        ids.append(saved['id'])
        print(strategy, result['metrics']['total_trades'], 'operaciones; guardado y Monte Carlo correctos')
    held = call('/api/holdout', {'dataset_id': dataset['id'], 'config': {'initial_balance': 10000}, 'split': .7})
    assert 'test' not in held
    revealed = call('/api/holdout/reveal', {'ticket_id': held['ticket_id']})
    assert revealed['test']['config']['trade_start'] == held['boundary']
    assert revealed['train']['config']['trade_end'] == held['boundary']
    print('Reserva temporal y reglas congeladas correctas')
    print('Ejemplos guardados:', ', '.join(ids))
