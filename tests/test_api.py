import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import server


class LaboratoryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_state = server.STATE
        self.old_runs = server.RUNS
        server.STATE = Path(self.temp.name)
        server.RUNS = Path(self.temp.name) / 'runs'
        server.RUNS.mkdir()
        server.RESULTS.clear()
        server.HOLDOUTS.clear()

    def tearDown(self):
        server.STATE = self.old_state
        server.RUNS = self.old_runs
        self.temp.cleanup()

    def test_snapshot_preserves_rules_and_results(self):
        original = server.remember({'metrics': {'net_pnl': -12.5}, 'config': {'initial_balance': 750},
            'dataset': {'id': 'example', 'synthetic': True}, 'code': 'def signal(ctx):\n    return 0',
            'trades': [], 'curve': [], 'warnings': []})
        saved = server.save_experiment({'result_id': original['id'], 'name': 'Hipótesis A', 'hypothesis': 'Sin filtro ADX'})
        server.RESULTS.clear()
        reloaded = server.get_result(saved['id'])
        self.assertEqual(reloaded['config']['initial_balance'], 750)
        self.assertEqual(reloaded['metrics']['net_pnl'], -12.5)
        self.assertEqual(reloaded['hypothesis'], 'Sin filtro ADX')
        self.assertEqual(len(server.experiment_list()), 1)
        json.dumps(reloaded, allow_nan=False)

    def test_path_traversal_rejected(self):
        for malicious in ('../server.py', '..\\server.py', '/tmp/file', '', 'a' * 101):
            with self.assertRaises(ValueError):
                server.valid_id(malicious)

    def test_host_and_origin_restricted(self):
        handler = object.__new__(server.Handler)
        failures = []
        handler.send = lambda *args: failures.append(args)
        handler.headers = {'Host': f'127.0.0.1:{server.APP_PORT}'}
        self.assertTrue(handler.allowed())
        handler.headers['Origin'] = 'https://external.example'
        self.assertFalse(handler.allowed())
        handler.headers = {'Host': f'attacker.example:{server.APP_PORT}'}
        self.assertFalse(handler.allowed())
        self.assertEqual([x[0] for x in failures], [403, 403])

    def test_holdout_freezes_config_before_reveal(self):
        origin = datetime(2024, 1, 1, tzinfo=timezone.utc)
        bars = [{'time': (origin + timedelta(hours=i)).isoformat().replace('+00:00', 'Z')} for i in range(200)]
        calls = []
        def fake_run(payload):
            calls.append(payload)
            return {'metrics': {}, 'config': payload['config']}
        request = {'dataset_id': 'demo', 'config': {'initial_balance': 812, 'risk_pct': 0.4}, 'code': 'original', 'split': 0.7}
        with patch.object(server, 'dataset_bars', return_value=(bars, {})), patch.object(server, 'run', side_effect=fake_run):
            prepared = server.holdout(request)
            self.assertEqual(len(calls), 1)
            self.assertNotIn('test', prepared)
            revealed = server.holdout({'ticket_id': prepared['ticket_id'], 'config': {'risk_pct': 9}, 'code': 'changed'}, reveal=True)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[-1]['config']['risk_pct'], 0.4)
            self.assertEqual(calls[-1]['code'], 'original')
            self.assertEqual(calls[-1]['config']['trade_start'], prepared['boundary'])
            self.assertEqual(calls[0]['config']['trade_end'], prepared['boundary'])
            self.assertEqual(revealed['train']['config']['initial_balance'], 812)
            server.holdout({'ticket_id': prepared['ticket_id']}, reveal=True)
            self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
