"""LOCAL TEST HARNESS ONLY — serves the real connector app backed by FakeMT5 (simulated
prices) so the Linux stack and dashboard can be exercised without a Windows terminal.
Never deploy this. Production uses run.py with the MetaTrader5 package."""
import os
import sys

import numpy as np
import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from connector.adapter import MT5Adapter  # noqa: E402
from connector.app import ConnectorConfig, create_app  # noqa: E402
from fake_mt5 import FakeMT5  # noqa: E402

print("!!! SIMULATED MT5 (FakeMT5) — TEST HARNESS, NOT A BROKER CONNECTION !!!", file=sys.stderr)
fake = FakeMT5(login=int(os.environ.get("RR_MT5_LOGIN", "1001")), server=os.environ.get("RR_MT5_SERVER", "Demo-Server"))
base = fake.copy_rates_from_pos
TF_SEC = {fake.TIMEFRAME_M1: 60, fake.TIMEFRAME_M5: 300, fake.TIMEFRAME_M15: 900, fake.TIMEFRAME_M30: 1800,
          fake.TIMEFRAME_H1: 3600, fake.TIMEFRAME_H4: 14400, fake.TIMEFRAME_D1: 86400}


def rates(name, tf, start, count):
    arr = base(name, tf, start, count)
    sec = TF_SEC.get(tf, 3600)
    now = int(__import__("time").time()) // sec * sec
    arr["time"] = now - (count - 1 - np.arange(count)) * sec
    rng = np.random.default_rng(abs(hash((name, tf))) % 2**32)
    p0 = arr["close"][0]
    regime = np.sign(np.sin(np.arange(count) / 90.0))
    close = p0 * np.exp(np.cumsum(rng.normal(regime * 0.00015, 0.0012, count)))
    arr["close"] = close
    arr["open"] = np.r_[close[0], close[:-1]]
    arr["high"] = np.maximum(arr["open"], close) * (1 + abs(rng.normal(0, 0.0006, count)))
    arr["low"] = np.minimum(arr["open"], close) * (1 - abs(rng.normal(0, 0.0006, count)))
    arr["tick_volume"] = rng.integers(100, 3000, count)
    s = fake.symbols[name]
    s["bid"] = float(close[-1]); s["ask"] = float(close[-1]) + 10 * s["point"]
    return arr


fake.copy_rates_from_pos = rates
cfg = ConnectorConfig(os.environ["RR_BRIDGE_SHARED_SECRET"], fake.login, fake.server,
                      journal_path=os.environ.get("RR_JOURNAL_PATH", "/tmp/rr-fake-journal.db"))
uvicorn.run(create_app(cfg, MT5Adapter(fake), connect=False), host="127.0.0.1", port=int(os.environ.get("RR_PORT", "8443")))
