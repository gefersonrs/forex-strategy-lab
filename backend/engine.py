"""Transparent, single-position major-FX backtester for a USD account.

Signals use completed UTC candles; orders enter at the next available source
bar's open. No exchange, broker, network or external execution is used.
"""
from __future__ import annotations

import ast
import math
import random
import statistics
from datetime import datetime, timezone
from collections import deque

PIP = 0.0001
LOT = 100000.0
TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}
SYMBOLS = {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF"}


def default_config():
    return {
        "strategy": "trend", "timeframe": "4h", "initial_balance": 500.0,
        "risk_pct": 0.5, "reward_risk": 3.0, "ema_period": 200,
        "atr_period": 14, "atr_multiplier": 2.0, "adx_period": 14,
        "adx_threshold": 20.0, "daily_filter": True, "daily_ema_period": 200,
        "breakout_period": 20, "breakout_atr_buffer": 0.1,
        "break_even_r": 1.5, "cooldown_bars": 3, "leverage": 30.0,
        "lot_step": 1000, "min_units": 1000, "commission_per_lot_side": 3.5,
        "spread_pips": 0.2, "slippage_pips": 0.1, "swap_per_lot_day": 0.0,
        "random_seed": 42, "random_trade_probability": 1.0,
        "force_close": False, "trade_start": None, "trade_end": None,
        "custom_history_limit": 1000, "custom_params": {},
        "symbol": "EURUSD", "direction": "both", "ema_filter": True, "adx_filter": True,
        "stop_mode": "atr", "stop_pips": 20.0, "stop_percent": 1.0,
        "sizing_mode": "risk", "fixed_units": 1000,
        "input_price_type": "mid", "observed_bid_ask": None,
    }


DEFAULT_CODE = '''def signal(ctx):
    # Las series contienen solo velas cerradas, con la más reciente al final.
    if not ctx["ready"]:
        return 0
    close = ctx["close"][-1]
    ema = ctx["ema"][-1]
    adx = ctx["adx"][-1]
    if adx < ctx["params"]["adx_threshold"]:
        return 0
    if close > ema and ctx["plus_di"][-1] > ctx["minus_di"][-1]:
        if ctx["daily_bull"] and ctx["breakout_up"]:
            return 1
    if close < ema and ctx["minus_di"][-1] > ctx["plus_di"][-1]:
        if ctx["daily_bear"] and ctx["breakout_down"]:
            return -1
    return 0
'''


def _number(value, label, minimum, maximum, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: se requiere un número.")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{label}: debe estar entre {minimum} y {maximum}.")
    if integer and int(value) != value:
        raise ValueError(f"{label}: se requiere un entero.")
    return int(value) if integer else float(value)


def _timestamp(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Fecha ISO no válida: {value[:80]}") from exc
    else:
        raise ValueError("Las fechas deben ser cadenas ISO UTC.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _config(config):
    c = default_config()
    if config is not None:
        if not isinstance(config, dict):
            raise ValueError("La configuración debe ser un objeto.")
        unknown = set(config) - set(c)
        if unknown:
            raise ValueError("Parámetros desconocidos: " + ", ".join(sorted(unknown)))
        c.update(config)
    if c["strategy"] not in ("trend", "random", "custom"):
        raise ValueError("strategy debe ser trend, random o custom.")
    if c["timeframe"] not in TIMEFRAMES:
        raise ValueError("timeframe debe ser 1m, 5m, 15m, 30m, 1h, 4h o 1d.")
    if c["symbol"] not in SYMBOLS:
        raise ValueError("Símbolo no compatible: se admiten EURUSD, GBPUSD, AUDUSD, NZDUSD, USDJPY, USDCAD y USDCHF.")
    for key, choices in {"direction": ("both", "long", "short"),
                         "stop_mode": ("atr", "pips", "percent"),
                         "sizing_mode": ("risk", "fixed"),
                         "input_price_type": ("bid", "mid", "ask")}.items():
        if c[key] not in choices:
            raise ValueError(f"{key}: opciones admitidas {', '.join(choices)}.")
    if c["observed_bid_ask"] is not None and not isinstance(c["observed_bid_ask"], bool):
        raise ValueError("observed_bid_ask debe ser true, false o null.")
    bounds = {
        "initial_balance": (1, 1e9), "risk_pct": (0.01, 10),
        "reward_risk": (0.1, 20), "atr_multiplier": (0.1, 20),
        "adx_threshold": (0, 100), "breakout_atr_buffer": (0, 5),
        "break_even_r": (0, 20), "leverage": (1, 500),
        "commission_per_lot_side": (0, 1000), "spread_pips": (0, 100),
        "slippage_pips": (0, 100), "swap_per_lot_day": (-1000, 1000),
        "random_trade_probability": (0, 1), "stop_pips": (0.1, 10000), "stop_percent": (0.001, 20),
    }
    for key, limits in bounds.items():
        c[key] = _number(c[key], key, *limits)
    for key, limits in {
        "ema_period": (2, 1000), "atr_period": (2, 500),
        "adx_period": (2, 500), "daily_ema_period": (2, 1000),
        "breakout_period": (2, 500), "cooldown_bars": (0, 1000),
        "lot_step": (1, 100000), "min_units": (1, 10000000),
        "random_seed": (0, 4294967295), "custom_history_limit": (50, 5000), "fixed_units": (1, 1000000000),
    }.items():
        c[key] = _number(c[key], key, *limits, integer=True)
    for key in ("daily_filter", "force_close", "ema_filter", "adx_filter"):
        if not isinstance(c[key], bool):
            raise ValueError(f"{key} debe ser true o false.")
    for key in ("trade_start", "trade_end"):
        if c[key]:
            c[key] = _iso(_timestamp(c[key]))
        else:
            c[key] = None
    if c["trade_start"] and c["trade_end"] and c["trade_start"] >= c["trade_end"]:
        raise ValueError("trade_end debe ser posterior a trade_start.")
    if not isinstance(c["custom_params"], dict) or len(c["custom_params"]) > 50:
        raise ValueError("custom_params debe ser un objeto de hasta 50 números.")
    for key, value in c["custom_params"].items():
        if not isinstance(key, str) or len(key) > 80:
            raise ValueError("Nombre de parámetro personalizado no válido.")
        _number(value, key, -1e12, 1e12)
    return c


_ALLOWED_FUNCTIONS = {"abs": abs, "min": min, "max": max, "round": round,
                      "sum": sum, "len": len, "int": int, "float": float}
_ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Assign, ast.If,
    ast.Return, ast.Expr, ast.Pass, ast.Name, ast.Load, ast.Store, ast.Constant,
    ast.Subscript, ast.Slice, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.IfExp, ast.Call, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
    ast.Mod, ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq, ast.NotEq,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
)


def _parse_code(code):
    if not isinstance(code, str) or len(code) > 12000:
        raise ValueError("El código debe contener como máximo 12000 caracteres.")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"Sintaxis no válida en línea {exc.lineno}: {exc.msg}") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 500:
        raise ValueError("La estrategia supera 500 elementos de sintaxis.")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("Define únicamente def signal(ctx).")
    fn = tree.body[0]
    if fn.name != "signal" or fn.decorator_list or fn.returns:
        raise ValueError("Se requiere def signal(ctx), sin decoradores ni anotaciones.")
    a = fn.args
    if (len(a.args) != 1 or a.args[0].arg != "ctx" or a.args[0].annotation or
            a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg or a.defaults):
        raise ValueError("signal debe recibir un único argumento llamado ctx.")
    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"No se permite {type(node).__name__}; usa variables, if y return.")
        if isinstance(node, ast.FunctionDef) and node is not fn:
            raise ValueError("No se permiten funciones anidadas.")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ValueError("No se permiten nombres que comiencen con guion bajo.")
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise ValueError("Solo se permite asignar a una variable local.")
            if node.targets[0].id in ("ctx", "signal") or node.targets[0].id in _ALLOWED_FUNCTIONS:
                raise ValueError("No se pueden sobrescribir ctx ni las funciones permitidas.")
        if isinstance(node, ast.Call):
            if (not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS
                    or node.keywords or len(node.args) > 8):
                raise ValueError("Solo se permiten abs, min, max, round, sum, len, int y float.")
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (str, int, float, bool, type(None))):
                raise ValueError("Constante no permitida.")
            if isinstance(node.value, str) and len(node.value) > 200:
                raise ValueError("Las cadenas deben tener como máximo 200 caracteres.")
            if isinstance(node.value, (int, float)):
                if abs(node.value) > 1e15 or not math.isfinite(node.value):
                    raise ValueError("Constante numérica demasiado grande.")
    return fn


def validate_code(code):
    """Raise ValueError for unsafe/invalid code, otherwise return a UI status."""
    _parse_code(code)
    return {"valid": True, "message": "Estrategia válida: Python restringido, sin acceso a archivos ni red."}


class _Return(Exception):
    def __init__(self, value):
        self.value = value


def _custom_signal(fn, ctx):
    """Interpret the allowed AST; user input is never passed to exec/eval."""
    env = {"ctx": ctx}
    budget = [2000]

    def bounded(value):
        if isinstance(value, (int, float)) and (not math.isfinite(value) or abs(value) > 1e15):
            raise ValueError("Resultado numérico fuera de límites.")
        return value

    def expr(node):
        budget[0] -= 1
        if budget[0] < 0:
            raise ValueError("La estrategia excedió el límite de operaciones.")
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise ValueError(f"Variable desconocida: {node.id}")
            return env[node.id]
        if isinstance(node, ast.Subscript):
            obj = expr(node.value)
            if not isinstance(obj, (dict, tuple, list)):
                raise ValueError("Solo se pueden indexar series y objetos del contexto.")
            return obj[expr(node.slice)]
        if isinstance(node, ast.Slice):
            return slice(expr(node.lower) if node.lower else None,
                         expr(node.upper) if node.upper else None,
                         expr(node.step) if node.step else None)
        if isinstance(node, ast.UnaryOp):
            value = expr(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if not isinstance(value, (int, float)):
                raise ValueError("Las operaciones aritméticas requieren números.")
            return bounded(-value if isinstance(node.op, ast.USub) else +value)
        if isinstance(node, ast.BinOp):
            left, right = expr(node.left), expr(node.right)
            if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                raise ValueError("Las operaciones aritméticas requieren números.")
            if isinstance(node.op, ast.Add): value = left + right
            elif isinstance(node.op, ast.Sub): value = left - right
            elif isinstance(node.op, ast.Mult): value = left * right
            elif isinstance(node.op, ast.Div): value = left / right
            elif isinstance(node.op, ast.FloorDiv): value = left // right
            else: value = left % right
            return bounded(value)
        if isinstance(node, ast.BoolOp):
            for child in node.values:
                value = expr(child)
                if isinstance(node.op, ast.And) and not value: return value
                if isinstance(node.op, ast.Or) and value: return value
            return value
        if isinstance(node, ast.Compare):
            left = expr(node.left)
            for op, child in zip(node.ops, node.comparators):
                right = expr(child)
                if isinstance(op, ast.Eq): ok = left == right
                elif isinstance(op, ast.NotEq): ok = left != right
                elif isinstance(op, ast.Lt): ok = left < right
                elif isinstance(op, ast.LtE): ok = left <= right
                elif isinstance(op, ast.Gt): ok = left > right
                elif isinstance(op, ast.GtE): ok = left >= right
                elif isinstance(op, ast.Is): ok = left is right
                else: ok = left is not right
                if not ok: return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return expr(node.body if expr(node.test) else node.orelse)
        if isinstance(node, ast.Call):
            args = [expr(arg) for arg in node.args]
            name = node.func.id
            if name == "len":
                if len(args) != 1 or not isinstance(args[0], (tuple, list, dict)):
                    raise ValueError("len requiere una serie o un objeto.")
            elif name in ("min", "max", "sum") and len(args) == 1 and isinstance(args[0], (tuple, list)):
                if not all(isinstance(x, (int, float)) for x in args[0]):
                    raise ValueError(f"{name} requiere una serie numérica sin valores nulos.")
            elif not all(isinstance(x, (int, float)) for x in args):
                raise ValueError(f"{name} requiere argumentos numéricos.")
            if name == "round" and len(args) == 2 and abs(args[1]) > 15:
                raise ValueError("La precisión de round está limitada a 15.")
            return bounded(_ALLOWED_FUNCTIONS[name](*args))
        raise ValueError("Expresión no permitida.")

    def statements(items):
        for node in items:
            if isinstance(node, ast.Return): raise _Return(expr(node.value) if node.value else 0)
            if isinstance(node, ast.Assign): env[node.targets[0].id] = expr(node.value)
            elif isinstance(node, ast.If): statements(node.body if expr(node.test) else node.orelse)
            elif isinstance(node, ast.Expr): expr(node.value)

    try:
        statements(fn.body)
        value = 0
    except _Return as returned:
        value = returned.value
    except (KeyError, IndexError, TypeError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError(f"Error en signal(ctx): {exc}") from exc
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value not in (-1, 0, 1):
        raise ValueError("signal(ctx) debe devolver -1, 0 o 1.")
    return int(value)


class _EMA:
    def __init__(self, period):
        self.period, self.count, self.total, self.value = period, 0, 0.0, None

    def update(self, value):
        self.count += 1
        if self.count <= self.period:
            self.total += value
            if self.count == self.period: self.value = self.total / self.period
        else:
            self.value += 2 / (self.period + 1) * (value - self.value)
        return self.value


class _Wilder:
    def __init__(self, period):
        self.period, self.count, self.total, self.value = period, 0, 0.0, None

    def update(self, value):
        self.count += 1
        if self.count <= self.period:
            self.total += value
            if self.count == self.period: self.value = self.total / self.period
        else:
            self.value = (self.value * (self.period - 1) + value) / self.period
        return self.value


class _Indicators:
    def __init__(self, c):
        self.ema = _EMA(c["ema_period"])
        self.atr = _Wilder(c["atr_period"])
        self.tr = _Wilder(c["adx_period"])
        self.pdm = _Wilder(c["adx_period"])
        self.mdm = _Wilder(c["adx_period"])
        self.adx = _Wilder(c["adx_period"])
        self.previous = None

    def update(self, bar):
        prior = self.previous
        tr = bar["high"] - bar["low"]
        up = down = 0.0
        if prior:
            tr = max(tr, abs(bar["high"] - prior["close"]), abs(bar["low"] - prior["close"]))
            up, down = bar["high"] - prior["high"], prior["low"] - bar["low"]
        positive = up if up > down and up > 0 else 0.0
        negative = down if down > up and down > 0 else 0.0
        st = self.tr.update(tr)
        pdm, mdm = self.pdm.update(positive), self.mdm.update(negative)
        pdi = 100 * pdm / st if st else (0.0 if st == 0 else None)
        mdi = 100 * mdm / st if st else (0.0 if st == 0 else None)
        adx = None
        if pdi is not None:
            dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0
            adx = self.adx.update(dx)
        self.previous = bar
        return {"ema": self.ema.update(bar["close"]), "atr": self.atr.update(tr),
                "adx": adx, "plus_di": pdi, "minus_di": mdi}


def _prepare_bars(bars, c):
    if not isinstance(bars, (list, tuple)) or len(bars) < 2:
        raise ValueError("Se necesitan al menos dos velas OHLC.")
    prepared, last_t, fallback = [], None, 0
    spread = c["spread_pips"] * (0.01 if c["symbol"].endswith("JPY") else PIP)
    for index, raw in enumerate(bars):
        try:
            ts = _timestamp(raw["time"])
            if last_t is not None and ts <= last_t:
                raise ValueError("Las velas deben estar ordenadas y no contener fechas duplicadas.")
            row = {"t": ts}
            for key in ("open", "high", "low", "close"):
                row[key] = _number(raw[key], key, 0.000001, 1000)
            for prefix in ("", "bid_", "ask_"):
                if prefix and not all(prefix + k in raw and raw[prefix + k] is not None for k in ("open", "high", "low", "close")):
                    continue
                if prefix:
                    for key in ("open", "high", "low", "close"):
                        row[prefix + key] = _number(raw[prefix + key], prefix + key, 0.000001, 1000)
                if row[prefix + "high"] < max(row[prefix + "open"], row[prefix + "close"], row[prefix + "low"]):
                    raise ValueError(f"{prefix}high es menor que open/close/low.")
                if row[prefix + "low"] > min(row[prefix + "open"], row[prefix + "close"]):
                    raise ValueError(f"{prefix}low es mayor que open/close.")
            if c["observed_bid_ask"] is False or "bid_open" not in row or "ask_open" not in row:
                fallback += 1
                for key in ("open", "high", "low", "close"):
                    if c["input_price_type"] == "bid":
                        original = row.get("bid_" + key, row[key])
                        row["bid_" + key], row["ask_" + key] = original, original + spread
                    elif c["input_price_type"] == "ask":
                        original = row.get("ask_" + key, row[key])
                        row["bid_" + key], row["ask_" + key] = original - spread, original
                    else:
                        row["bid_" + key], row["ask_" + key] = row[key] - spread / 2, row[key] + spread / 2
                    if row["bid_" + key] <= 0:
                        raise ValueError("El spread produce un precio bid no positivo.")
                    row[key] = (row["bid_" + key] + row["ask_" + key]) / 2
            else:
                for key in ("open", "high", "low", "close"):
                    if row["ask_" + key] < row["bid_" + key]:
                        raise ValueError("Ask no puede ser menor que bid.")
            prepared.append(row)
            last_t = ts
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Vela {index + 1}: faltan datos OHLC válidos.") from exc
        except ValueError as exc:
            raise ValueError(f"Vela {index + 1}: {exc}") from exc
    deltas = [prepared[i]["t"] - prepared[i-1]["t"] for i in range(1, len(prepared))]
    interval = statistics.median(deltas)
    if interval > TIMEFRAMES[c["timeframe"]]:
        raise ValueError("La resolución de los datos debe ser igual o menor que el marco de señales.")
    if interval <= 0 or interval > 86400:
        raise ValueError("Intervalo de datos no válido.")
    if any(delta < interval for delta in deltas):
        interval = min(deltas)
    return prepared, interval, fallback


def _aggregate(bars, seconds):
    result = []
    for row in bars:
        bucket = math.floor(row["t"] / seconds) * seconds
        if not result or result[-1]["t"] != bucket:
            result.append({"t": bucket, "end": bucket + seconds,
                           **{key: row[key] for key in ("open", "high", "low", "close")}})
        else:
            b = result[-1]
            b["high"] = max(b["high"], row["high"])
            b["low"] = min(b["low"], row["low"])
            b["close"] = row["close"]
    return result


def _sample_curve(rows, limit=2500):
    if len(rows) <= limit: return rows
    result = [rows[0]]
    buckets = (limit - 2) // 2
    span = (len(rows) - 2) / buckets
    for i in range(buckets):
        start = 1 + int(i * span)
        end = 1 + int((i + 1) * span)
        indices = range(start, min(end, len(rows) - 1))
        if end <= start: continue
        lo = min(indices, key=lambda j: rows[j]["equity"])
        hi = max(indices, key=lambda j: rows[j]["equity"])
        for j in sorted({lo, hi}): result.append(rows[j])
    result.append(rows[-1])
    return result


def _sample_candles(rows, limit=5000):
    if len(rows) <= limit: return rows
    indices = {round(i * (len(rows) - 1) / (limit - 1)) for i in range(limit)}
    return [rows[i] for i in sorted(indices)]


def run_backtest(bars, config=None, code=None):
    c = _config(config)
    fn = _parse_code(code if code is not None else DEFAULT_CODE) if c["strategy"] == "custom" else None
    source, interval, fallback = _prepare_bars(bars, c)
    start = _timestamp(c["trade_start"]) if c["trade_start"] else source[0]["t"]
    end = _timestamp(c["trade_end"]) if c["trade_end"] else source[-1]["t"] + interval
    if start >= source[-1]["t"] + interval or end <= source[0]["t"]:
        raise ValueError("El período de prueba no contiene velas.")
    signal_bars = _aggregate(source, TIMEFRAMES[c["timeframe"]])
    daily_bars = _aggregate(source, 86400)
    indicators, daily_ema = _Indicators(c), _EMA(c["daily_ema_period"])
    daily_index = signal_index = 0
    daily_close = daily_value = None
    history, candles, trades, curve = [], [], [], []
    balance = peak = equity = c["initial_balance"]
    max_dd = max_dd_pct = total_commission = total_swap = 0.0
    pos = None
    last_exit_signal = -1000000
    randomizer = random.Random(c["random_seed"])
    pip = 0.01 if c["symbol"].endswith("JPY") else PIP
    slip = c["slippage_pips"] * pip
    base_usd = c["symbol"].startswith("USD")
    last_row = None
    exposed_bars = active_bars = 0
    diagnostics = {"source_interval_seconds": interval, "source_bars": len(source),
                   "completed_signal_bars": 0, "ready_signal_bars": 0,
                   "signals": 0, "margin_skips": 0, "min_size_skips": 0,
                   "ambiguous_exits": 0, "cooldown_skips": 0,
                   "fallback_spread_bars": fallback, "bankruptcy_skips": 0,
                   "custom_history_limit": c["custom_history_limit"], "direction_skips": 0, "invalid_price_skips": 0,
                   "pip_size": pip, "pnl_conversion": "quote_to_usd_at_exit" if base_usd else "quote_is_usd"}
    warnings = [
        f"Simulación de {c['symbol']} en cuenta USD. No envía órdenes reales.",
        "Las señales usan cierres confirmados; la entrada ocurre en la apertura siguiente disponible.",
        "El recorrido intravela es desconocido: cuando SL y TP coinciden, se aplica SL primero.",
        "MAE/MFE usan extremos de la vela y son cotas aproximadas en la vela de salida.",
        "Swap diario UTC aproximado, sin calendario específico del bróker. Un valor positivo es un cargo.",
        "El margen se comprueba al abrir; no se simulan liquidaciones forzosas ni cambios históricos de apalancamiento.",
    ]
    if fallback:
        warnings.append(f"Las cotizaciones estimadas se reconstruyen desde {c['input_price_type']} con el spread fijo configurado.")
    if c["sizing_mode"] == "fixed":
        warnings.append("El tamaño fijo usa las unidades indicadas; el riesgo resultante no está limitado por risk_pct.")
    if interval > 60:
        warnings.append(f"Resolución fuente de {interval / 60:g} minutos: las salidas se aproximan con ese OHLC, no con ticks.")
    if c["break_even_r"]:
        warnings.append("El stop a break-even se activa en la vela fuente siguiente; cubre el precio de entrada, no las comisiones ni el swap.")
    if c["strategy"] == "custom":
        warnings.append(f"El editor interpreta Python restringido; ctx contiene hasta {c['custom_history_limit']} velas cerradas.")

    def record_equity(row, timestamp):
        nonlocal equity, peak, max_dd, max_dd_pct
        equity = balance
        if pos:
            mark = row["bid_close"] if pos["side_int"] == 1 else row["ask_close"]
            equity += pos["side_int"] * (mark - pos["entry"]) * pos["units"] / (mark if base_usd else 1)
        peak = max(peak, equity)
        dd = max(0.0, peak - equity)
        pct = dd / peak * 100 if peak > 0 else 0.0
        max_dd, max_dd_pct = max(max_dd, dd), max(max_dd_pct, pct)
        curve.append({"time": _iso(timestamp), "balance": balance, "equity": equity, "drawdown_pct": pct})

    def exit_position(row, price, reason, exit_slippage, ambiguous=False):
        nonlocal pos, balance, total_commission, last_exit_signal
        side = pos["side_int"]
        fee = pos["lots"] * c["commission_per_lot_side"]
        gross = side * (price - pos["entry"]) * pos["units"] / (price if base_usd else 1)
        balance += gross - fee
        total_commission += fee
        commission = pos["entry_commission"] + fee
        pnl = gross - commission - pos["swap_cost"]
        exit_spread = max(0.0, row["ask_open"] - row["bid_open"])
        trade = {"id": len(trades) + 1, "side": "long" if side == 1 else "short",
                 "entry_time": pos["entry_time"], "exit_time": _iso(row["t"]),
                 "entry": pos["entry"], "exit": price, "sl": pos["initial_sl"],
                 "tp": pos["tp"], "final_sl": pos["sl"], "units": pos["units"],
                 "lots": pos["lots"], "risk_amount": pos["risk_amount"],
                 "pnl": pnl, "gross_pnl": gross, "r": pnl / pos["risk_amount"],
                 "commission": commission, "swap_cost": pos["swap_cost"],
                 "spread_cost": pos["units"] * (pos["entry_spread"] / (pos["entry"] if base_usd else 1) + exit_spread / (price if base_usd else 1)) / 2,
                 "slippage_cost": pos["units"] * (slip / (pos["entry"] if base_usd else 1) + exit_slippage / (price if base_usd else 1)),
                 "mae": pos["mae"], "mfe": pos["mfe"],
                 "mae_r": pos["mae"] / pos["risk_amount"],
                 "mfe_r": pos["mfe"] / pos["risk_amount"],
                 "exit_reason": reason, "entry_reason": pos["entry_reason"],
                 "entry_indicators": pos["entry_indicators"],
                 "ambiguous_exit": ambiguous, "excursions_are_bounds": True,
                 "break_even_activated": pos["break_even_activated"]}
        trades.append(trade)
        last_exit_signal = signal_index - 1
        pos = None

    def advance_signal(bar):
        nonlocal daily_index, daily_close, daily_value
        while daily_index < len(daily_bars) and daily_bars[daily_index]["end"] <= bar["end"]:
            day = daily_bars[daily_index]
            daily_close = day["close"]
            daily_value = daily_ema.update(daily_close)
            daily_index += 1
        values = indicators.update(bar)
        n = c["breakout_period"]
        prior_high = max((b["high"] for b in history[-n:]), default=None) if len(history) >= n else None
        prior_low = min((b["low"] for b in history[-n:]), default=None) if len(history) >= n else None
        prev = history[-1] if history else None
        atr = values["atr"]
        up = down = False
        if (prior_high is not None and prev and prev["breakout_high"] is not None and
                (c["breakout_atr_buffer"] == 0 or (atr is not None and prev["atr"] is not None))):
            buffer = c["breakout_atr_buffer"]
            up = bar["close"] > prior_high + buffer * (atr or 0) and prev["close"] <= prev["breakout_high"] + buffer * (prev["atr"] or 0)
            down = bar["close"] < prior_low - buffer * (atr or 0) and prev["close"] >= prev["breakout_low"] - buffer * (prev["atr"] or 0)
        stop_ready = c["stop_mode"] != "atr" or (atr is not None and atr > 0)
        ready = stop_ready if c["strategy"] == "random" else (
            stop_ready and (not c["ema_filter"] or values["ema"] is not None) and
            (not c["adx_filter"] or values["adx"] is not None) and prior_high is not None and
            (c["breakout_atr_buffer"] == 0 or atr is not None) and
            (not c["daily_filter"] or daily_value is not None))
        bull = not c["daily_filter"] or (daily_value is not None and daily_close > daily_value)
        bear = not c["daily_filter"] or (daily_value is not None and daily_close < daily_value)
        item = {"time": _iso(bar["t"]), **{k: bar[k] for k in ("open", "high", "low", "close")},
                **values, "breakout_high": prior_high, "breakout_low": prior_low,
                "breakout_up": up, "breakout_down": down, "ready": ready,
                "daily_bull": bull, "daily_bear": bear}
        history.append(item)
        candles.append(item)
        diagnostics["completed_signal_bars"] += 1
        if ready and bar["end"] >= start and bar["end"] < end:
            diagnostics["ready_signal_bars"] += 1
        return item

    for row in source:
        now = row["t"]
        if now + interval > end: break
        latest = None
        while signal_index < len(signal_bars) and signal_bars[signal_index]["end"] <= now:
            latest = advance_signal(signal_bars[signal_index])
            signal_index += 1
        if now < start: continue
        active_bars += 1
        if not curve:
            curve.append({"time": _iso(now), "balance": balance, "equity": equity, "drawdown_pct": 0.0})
        if pos:
            days = max(0, math.floor(now / 86400) - pos["swap_day"])
            if days:
                swap = days * c["swap_per_lot_day"] * pos["lots"]
                pos["swap_cost"] += swap
                balance -= swap
                total_swap += swap
                pos["swap_day"] = math.floor(now / 86400)
            if pos["pending_be"]:
                pos["sl"] = pos["entry"]
                pos["break_even_activated"] = True
                pos["pending_be"] = False
        custom_ready = latest is not None and c['strategy'] == 'custom' and (
            c['stop_mode'] != 'atr' or (latest['atr'] is not None and latest['atr'] > 0))
        if pos is None and latest and (latest["ready"] or custom_ready):
            if signal_index - 1 - last_exit_signal < c["cooldown_bars"]:
                diagnostics["cooldown_skips"] += 1
            elif balance <= 0:
                diagnostics["bankruptcy_skips"] += 1
            else:
                side = 0
                if c["strategy"] == "random":
                    if randomizer.random() < c["random_trade_probability"]:
                        side = 1 if randomizer.random() < 0.5 else -1
                elif c["strategy"] == "trend":
                    if not c["adx_filter"] or latest["adx"] >= c["adx_threshold"]:
                        if ((not c["ema_filter"] or latest["close"] > latest["ema"]) and
                                (not c["adx_filter"] or latest["plus_di"] > latest["minus_di"])
                                and latest["daily_bull"] and latest["breakout_up"]): side = 1
                        elif ((not c["ema_filter"] or latest["close"] < latest["ema"]) and
                              (not c["adx_filter"] or latest["minus_di"] > latest["plus_di"])
                              and latest["daily_bear"] and latest["breakout_down"]): side = -1
                else:
                    subset = history[-c["custom_history_limit"]:]
                    ctx = {key: tuple(x[key] for x in subset) for key in
                           ("time", "open", "high", "low", "close", "ema", "atr", "adx", "plus_di", "minus_di")}
                    ctx.update({key: latest[key] for key in ("breakout_high", "breakout_low", "breakout_up",
                                "breakout_down", "daily_bull", "daily_bear", "ready")})
                    ctx["params"] = {**c, **c["custom_params"]}
                    side = _custom_signal(fn, ctx)
                if side and ((c["direction"] == "long" and side == -1) or (c["direction"] == "short" and side == 1)):
                    diagnostics["direction_skips"] += 1
                    side = 0
                if side:
                    diagnostics["signals"] += 1
                    reference = row["ask_open"] if side == 1 else row["bid_open"]
                    if c["stop_mode"] == "atr": distance = latest["atr"] * c["atr_multiplier"]
                    elif c["stop_mode"] == "pips": distance = c["stop_pips"] * pip
                    else: distance = reference * c["stop_percent"] / 100
                    entry = reference + side * slip
                    stop_exit = reference - side * (distance + slip)
                    target = reference + side * distance * c["reward_risk"]
                    if min(entry, stop_exit, target) <= 0:
                        diagnostics["invalid_price_skips"] += 1
                        record_equity(row, now + interval)
                        last_row = row
                        continue
                    cost_per_unit = (distance + 2 * slip) / (stop_exit if base_usd else 1) + 2 * c["commission_per_lot_side"] / LOT
                    budget = balance * c["risk_pct"] / 100
                    raw_units = budget / cost_per_unit if c["sizing_mode"] == "risk" else c["fixed_units"]
                    units = math.floor(raw_units / c["lot_step"]) * c["lot_step"]
                    required_margin = units * (1 if base_usd else entry) / c["leverage"]
                    entry_fee = units / LOT * c["commission_per_lot_side"]
                    if units < c["min_units"]:
                        diagnostics["min_size_skips"] += 1
                    elif required_margin + entry_fee > balance:
                        diagnostics["margin_skips"] += 1
                    else:
                        balance -= entry_fee
                        total_commission += entry_fee
                        pos = {"side_int": side, "entry": entry, "entry_time": _iso(now),
                               "sl": reference - side * distance, "initial_sl": reference - side * distance,
                               "tp": target,
                               "units": units, "lots": units / LOT, "risk_amount": units * cost_per_unit,
                               "risk_budget": budget, "entry_commission": entry_fee,
                               "entry_spread": max(0.0, row["ask_open"] - row["bid_open"]),
                               "swap_cost": 0.0, "swap_day": math.floor(now / 86400),
                               "mae": 0.0, "mfe": 0.0, "pending_be": False,
                               "break_even_activated": False, "distance": distance,
                               "margin": required_margin, "entry_reason": c["strategy"],
                               "entry_indicators": {k: latest[k] for k in ("ema", "atr", "adx", "plus_di", "minus_di", "daily_bull", "daily_bear")}}
        if pos:
            exposed_bars += 1
            side = pos["side_int"]
            prefix = "bid_" if side == 1 else "ask_"
            op, hi, lo = (row[prefix + k] for k in ("open", "high", "low"))
            favorable = (hi - pos["entry"]) if side == 1 else (pos["entry"] - lo)
            adverse = (pos["entry"] - lo) if side == 1 else (hi - pos["entry"])
            pos["mfe"] = max(pos["mfe"], favorable * pos["units"] / ((hi if side == 1 else lo) if base_usd else 1), 0.0)
            pos["mae"] = max(pos["mae"], adverse * pos["units"] / ((lo if side == 1 else hi) if base_usd else 1), 0.0)
            stop_open = op <= pos["sl"] if side == 1 else op >= pos["sl"]
            target_open = op >= pos["tp"] if side == 1 else op <= pos["tp"]
            stop_hit = lo <= pos["sl"] if side == 1 else hi >= pos["sl"]
            target_hit = hi >= pos["tp"] if side == 1 else lo <= pos["tp"]
            if stop_open:
                exit_position(row, op - side * slip, "gap_stop", slip)
            elif target_open:
                exit_position(row, op, "gap_target", 0.0)
            elif stop_hit:
                if target_hit: diagnostics["ambiguous_exits"] += 1
                reason = "break_even" if pos["break_even_activated"] else "stop_loss"
                exit_position(row, pos["sl"] - side * slip, reason, slip, target_hit)
            elif target_hit:
                exit_position(row, pos["tp"], "take_profit", 0.0)
            elif (c["break_even_r"] > 0 and not pos["break_even_activated"] and
                  favorable >= c["break_even_r"] * pos["distance"]):
                pos["pending_be"] = True
        record_equity(row, min(now + interval, end))
        last_row = row
    if last_row is None:
        raise ValueError("El período seleccionado no contiene aperturas de velas.")
    # The final completed signal candle is shown, but cannot create an order
    # without a following source open.
    final_end = min(last_row["t"] + interval, end)
    while signal_index < len(signal_bars) and signal_bars[signal_index]["end"] <= final_end:
        advance_signal(signal_bars[signal_index])
        signal_index += 1
    if pos and c["force_close"]:
        side = pos["side_int"]
        close_quote = last_row["bid_close"] if side == 1 else last_row["ask_close"]
        exit_row = {**last_row, "t": final_end}
        exit_position(exit_row, close_quote - side * slip, "end_of_test", slip)
        curve.pop()
        record_equity(last_row, final_end)
    open_position = None
    if pos:
        side = pos["side_int"]
        mark = last_row["bid_close"] if side == 1 else last_row["ask_close"]
        open_position = {"side": "long" if side == 1 else "short", "entry_time": pos["entry_time"],
                         "entry": pos["entry"], "sl": pos["sl"], "tp": pos["tp"],
                         "units": pos["units"], "lots": pos["lots"], "margin": pos["margin"],
                         "risk_amount": pos["risk_amount"], "unrealized_pnl": side * (mark - pos["entry"]) * pos["units"] / (mark if base_usd else 1),
                         "commission": pos["entry_commission"], "swap_cost": pos["swap_cost"]}
        warnings.append("Existe una posición abierta al final; las estadísticas de operaciones solo incluyen las cerradas y equity incluye su valoración.")
    if diagnostics["ready_signal_bars"] == 0:
        warnings.append("No hay señales elegibles: falta calentamiento de indicadores, datos en el período o ATR positivo. El filtro diario requiere días completos suficientes.")
    if diagnostics["min_size_skips"]:
        warnings.append(f"Se omitieron {diagnostics['min_size_skips']} entradas por tamaño inferior al mínimo con el riesgo configurado.")
    if diagnostics["margin_skips"]:
        warnings.append(f"Se omitieron {diagnostics['margin_skips']} entradas por margen insuficiente.")
    gains = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    metrics = {"balance": balance, "equity": equity, "net_pnl": equity - c["initial_balance"],
               "closed_net_pnl": sum(t["pnl"] for t in trades),
               "net_return_pct": (equity / c["initial_balance"] - 1) * 100,
               "max_drawdown": max_dd, "max_drawdown_pct": max_dd_pct,
               "profit_factor": gains / losses if losses else None,
               "win_rate": sum(t["pnl"] > 0 for t in trades) / len(trades) * 100 if trades else 0.0,
               "expectancy_r": statistics.mean(t["r"] for t in trades) if trades else 0.0,
               "total_trades": len(trades), "total_commission": total_commission,
               "total_swap": total_swap, "exposure_pct": exposed_bars / active_bars * 100 if active_bars else 0.0}
    diagnostics.update({"curve_points_full": len(curve), "candle_points_full": len(candles),
                        "curve_sampled": len(curve) > 2500, "candles_sampled": len(candles) > 5000,
                        "trade_start": _iso(max(start, source[0]["t"])), "trade_end": _iso(final_end)})
    if len(candles) > 5000:
        warnings.append("El gráfico muestra una muestra de hasta 5000 velas; las métricas utilizan todos los datos.")
    if len(curve) > 2500:
        warnings.append("La curva visual conserva extremos por tramos hasta 2500 puntos; el drawdown se calcula en cada cierre fuente.")
    return {"config": c, "metrics": metrics, "curve": _sample_curve(curve), "trades": trades,
            "candles": _sample_candles(candles), "warnings": warnings, "diagnostics": diagnostics,
            "open_position": open_position}


def _percentile(values, q):
    if not values: return None
    values = sorted(values)
    at = (len(values) - 1) * q
    lo, hi = math.floor(at), math.ceil(at)
    return values[lo] + (values[hi] - values[lo]) * (at - lo)


def monte_carlo(result, simulations=500, seed=42):
    """Bootstrap observed net R, resizing each trade at the configured risk.

    This is a conditional robustness illustration, not an independent estimate
    of future profitability; no price paths or market regimes are generated.
    """
    simulations = _number(simulations, "simulations", 20, 5000, integer=True)
    seed = _number(seed, "seed", 0, 4294967295, integer=True)
    c = _config(result.get("config"))
    fixed_size = c['sizing_mode'] == 'fixed'
    outcomes = [float(t['pnl'] if fixed_size else t["r"]) for t in result.get("trades", [])]
    if not outcomes:
        raise ValueError("Monte Carlo requiere al menos una operación cerrada.")
    if any(not math.isfinite(r) for r in outcomes):
        raise ValueError("Los resultados de operaciones deben ser finitos.")
    initial, risk = c["initial_balance"], c["risk_pct"] / 100
    rng = random.Random(seed)
    # Keep at most 501 chart steps; simulate every individual trade.
    count = len(outcomes)
    selected = sorted({round(i * count / min(count, 500)) for i in range(min(count, 500) + 1)})
    columns = {i: [] for i in selected}
    terminal, drawdowns = [], []
    ruined = 0
    for _ in range(simulations):
        balance = peak = initial
        maximum_dd = 0.0
        did_ruin = False
        columns[0].append(balance)
        for step in range(1, count + 1):
            outcome = rng.choice(outcomes)
            balance = max(0.0, balance + outcome if fixed_size else balance * (1 + risk * outcome)) if balance > 0 else 0.0
            peak = max(peak, balance)
            maximum_dd = max(maximum_dd, (peak - balance) / peak * 100)
            if balance <= initial * 0.5: did_ruin = True
            if step in columns: columns[step].append(balance)
        terminal.append(balance)
        drawdowns.append(maximum_dd)
        ruined += did_ruin
    def quantiles(values):
        return {"p5": _percentile(values, .05), "p50": _percentile(values, .5), "p95": _percentile(values, .95)}
    method_note = ("Bootstrap con reemplazo de PnL netos en dólares y tamaño fijo; presupone operaciones independientes." if fixed_size else
                   "Bootstrap con reemplazo de R netos observados y riesgo porcentual compuesto; supone invertir todo el presupuesto de riesgo y operaciones independientes.")
    warnings = [method_note,
                "No incorpora nuevos regímenes, incertidumbre de parámetros, mínimo de lote ni restricciones futuras de margen.",
                "La probabilidad de ruina aquí significa tocar el 50% del capital inicial; no es una predicción."]
    if count < 30:
        warnings.append("Menos de 30 operaciones: la muestra es demasiado pequeña para conclusiones estables.")
    return {"simulations": simulations, "seed": seed, "trade_count": count,
            "method": "bootstrap_fixed_net_pnl" if fixed_size else "bootstrap_net_r_compounded", "initial_balance": initial,
            "terminal": quantiles(terminal), "max_drawdown_pct": quantiles(drawdowns),
            "probability_loss_pct": sum(x < initial for x in terminal) / simulations * 100,
            "probability_ruin_pct": ruined / simulations * 100,
            "ruin_threshold_pct": 50, "curves": [{"trade": i, **quantiles(columns[i])} for i in selected],
            "warnings": warnings}
