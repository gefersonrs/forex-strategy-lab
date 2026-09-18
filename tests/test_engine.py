import json
import math
import unittest
from datetime import datetime, timedelta, timezone

from backend import engine


def bars(values):
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    return [{'time': (start + timedelta(hours=i)).isoformat(), 'open': o, 'high': h, 'low': l, 'close': c}
            for i, (o, h, l, c) in enumerate(values)]


def config(**kwargs):
    return {**engine.default_config(), 'strategy': 'custom', 'timeframe': '1h',
            'stop_mode': 'pips', 'stop_pips': 20, 'sizing_mode': 'fixed', 'fixed_units': 1000,
            'initial_balance': 10000, 'daily_filter': False, 'ema_filter': False, 'adx_filter': False,
            'breakout_atr_buffer': 0, 'break_even_r': 0, 'cooldown_bars': 0,
            'spread_pips': 2, 'slippage_pips': 1, **kwargs}


LONG = 'def signal(ctx):\n    if len(ctx["close"]) == 1:\n        return 1\n    return 0'
SHORT = LONG.replace('return 1', 'return -1')
FLAT = (1.1, 1.1001, 1.0999, 1.1)


class EngineExecutionTests(unittest.TestCase):
    def test_no_entry_on_signal_bar_and_ambiguous_stop_first(self):
        rows = bars([FLAT, (1.1, 1.108, 1.097, 1.10), FLAT])
        result = engine.run_backtest(rows, config(), LONG)
        trade = result['trades'][0]
        self.assertEqual(trade['entry_time'], '2024-01-02T01:00:00Z')
        self.assertEqual(trade['exit_reason'], 'stop_loss')
        self.assertTrue(trade['ambiguous_exit'])
        self.assertAlmostEqual(trade['pnl'], -2.27)
        self.assertAlmostEqual(result['metrics']['balance'], 9997.73)
        self.assertAlmostEqual(trade['r'], -1)

    def test_short_uses_ask_for_exit(self):
        result = engine.run_backtest(bars([FLAT, (1.1, 1.103, 1.099, 1.1), FLAT]), config(), SHORT)
        trade = result['trades'][0]
        self.assertEqual(trade['side'], 'short')
        self.assertAlmostEqual(trade['entry'], 1.0998)
        self.assertAlmostEqual(trade['exit'], 1.1020)
        self.assertAlmostEqual(trade['pnl'], -2.27)

    def test_gap_can_exceed_risk(self):
        result = engine.run_backtest(bars([FLAT, FLAT, (1.095, 1.096, 1.094, 1.095)]), config(), LONG)
        trade = result['trades'][0]
        self.assertEqual(trade['exit_reason'], 'gap_stop')
        self.assertLess(trade['pnl'], -trade['risk_amount'])

    def test_break_even_changes_only_next_bar(self):
        rows = bars([FLAT, (1.1, 1.104, 1.099, 1.101), (1.101, 1.102, 1.1, 1.101)])
        result = engine.run_backtest(rows, config(break_even_r=1.5), LONG)
        trade = result['trades'][0]
        self.assertEqual(trade['exit_time'], '2024-01-02T02:00:00Z')
        self.assertEqual(trade['exit_reason'], 'break_even')
        self.assertAlmostEqual(trade['pnl'], -0.17)

    def test_minimum_lot_and_margin_rejections(self):
        rows = bars([FLAT] * 6)
        risk = engine.run_backtest(rows, config(initial_balance=100, sizing_mode='risk', risk_pct=.1), LONG)
        self.assertEqual(risk['diagnostics']['min_size_skips'], 1)
        margin = engine.run_backtest(rows, config(initial_balance=10, fixed_units=10000, leverage=1), LONG)
        self.assertEqual(margin['diagnostics']['margin_skips'], 1)
        self.assertEqual(margin['metrics']['total_trades'], 0)

    def test_bid_import_spread_changes_per_experiment(self):
        rows = bars([FLAT] * 3)
        for row in rows:
            for key in ('open', 'high', 'low', 'close'):
                row['bid_' + key] = row[key]
                row['ask_' + key] = row[key] + .0001
        narrow = engine.run_backtest(rows, config(input_price_type='bid', observed_bid_ask=False, spread_pips=1, force_close=True), LONG)
        wide = engine.run_backtest(rows, config(input_price_type='bid', observed_bid_ask=False, spread_pips=3, force_close=True), LONG)
        self.assertAlmostEqual(narrow['metrics']['net_pnl'] - wide['metrics']['net_pnl'], .2)

    def test_usdjpy_conversion(self):
        flat = (150, 150.001, 149.999, 150)
        rows = bars([flat, (150, 150.8, 149.9, 150.6), flat])
        for code, direction in ((LONG, 1), (SHORT, -1)):
            with self.subTest(direction=direction):
                result = engine.run_backtest(rows, config(symbol='USDJPY', spread_pips=0, slippage_pips=0), code)
                trade = result['trades'][0]
                expected = direction * (trade['exit'] - trade['entry']) * 1000 / trade['exit'] - .07
                self.assertAlmostEqual(trade['pnl'], expected)
                self.assertEqual(result['diagnostics']['pip_size'], .01)

    def test_direction_filter_and_seed_reproducible(self):
        rows = bars([FLAT] * 10)
        blocked = engine.run_backtest(rows, config(direction='short'), LONG)
        self.assertEqual(blocked['diagnostics']['direction_skips'], 1)
        cfg = config(strategy='random', force_close=True)
        a, b = engine.run_backtest(rows, cfg), engine.run_backtest(rows, cfg)
        self.assertEqual(a, b)

    def test_future_values_do_not_change_past_trades(self):
        original = bars([FLAT, (1.1, 1.108, 1.097, 1.1), FLAT, FLAT])
        changed = [*original[:3], {**original[3], 'high': 100, 'low': .01}]
        a = engine.run_backtest(original, config(), LONG)
        b = engine.run_backtest(changed, config(), LONG)
        self.assertEqual(a['trades'], b['trades'])

    def test_resolution_cannot_be_invented(self):
        with self.assertRaises(ValueError):
            engine.run_backtest(bars([FLAT] * 4), config(timeframe='1m'), LONG)

    def test_custom_python_rejects_system_access(self):
        for code in ('import os', 'def signal(ctx):\n    return ctx.__class__',
                     'def signal(ctx):\n    while True:\n        pass',
                     'def signal(ctx):\n    return open("secret")'):
            with self.assertRaises(ValueError):
                engine.validate_code(code)

    def test_custom_signal_needs_no_unrelated_indicator_warmup(self):
        result = engine.run_backtest(bars([FLAT] * 3), config(daily_filter=True, force_close=True), LONG)
        self.assertEqual(result['metrics']['total_trades'], 1)
        json.dumps(result, allow_nan=False)

    def test_fixed_monte_carlo_uses_dollars(self):
        result = {'config': config(initial_balance=500), 'trades': [{'r': 10, 'pnl': 2}] * 3}
        mc = engine.monte_carlo(result, 100, 42)
        self.assertEqual(mc['method'], 'bootstrap_fixed_net_pnl')
        self.assertEqual(mc['terminal']['p50'], 506)
        self.assertEqual(mc, engine.monte_carlo(result, 100, 42))

    def test_percent_monte_carlo_compounds(self):
        result = {'config': config(initial_balance=500, sizing_mode='risk', risk_pct=1), 'trades': [{'r': 1, 'pnl': 2}] * 3}
        mc = engine.monte_carlo(result, 100, 42)
        self.assertAlmostEqual(mc['terminal']['p50'], 500 * 1.01 ** 3)


if __name__ == '__main__':
    unittest.main()
