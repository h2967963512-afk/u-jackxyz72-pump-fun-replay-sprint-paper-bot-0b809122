"""Entrypoint for the pump.fun replay bot (agent-fun/replay/v2).

The grader runs this as ``python3 -P -s /bot/main.py`` with
``PYTHONHASHSEED=0``. -P leaves this directory off sys.path, so both the
bot's own directory (for strategy.py) and vendor/ (for the protocol shim)
are added explicitly. The strategy itself lives in strategy.py; the wire
protocol lives in vendor/makefun_bot.py (the kit's Python SDK shim).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path lines)
from strategy import Strategy  # noqa: E402

_STRATEGY = Strategy()


def on_batch(state, batch):
    return _STRATEGY.on_batch(state, batch)


def on_done(state, done):
    account = done["account"]
    cash = int(account["cash_lamports"]) / makefun_bot.LAMPORTS_PER_SOL
    positions = account["positions"]
    print(
        f"done: cash {cash:.6f} SOL, open positions {len(positions)}, "
        f"batches answered",
        file=sys.stderr,
    )


if __name__ == "__main__":
    makefun_bot.run(on_batch, on_done=on_done)
