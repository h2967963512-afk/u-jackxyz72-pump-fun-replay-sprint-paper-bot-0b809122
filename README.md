# u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122

[![make.fun · u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![rank](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/h2967963512-afk/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/rank.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)
[![reward](https://make.fun/badge/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/h2967963512-afk/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122/reward.svg)](https://make.fun/bounty/u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122?utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122)

Graduated-momentum paper-trading bot for make.fun's "Pump.fun Replay Sprint — Paper Bot, Real Ranking" bounty. It trades 10 SOL of paper money on a 24-hour replay of pump.fun bonding-curve activity. Best net PnL wins.

## Strategy

Most pump.fun curves die young: a fresh launch is often a bundle that dumps within minutes. The coins that pay are the ones where real, broad buying keeps arriving after the launch noise fades. This bot only buys a curve once it has survived a minimum age and its recent tape shows accelerating, widely spread buy pressure with the price sitting near its recent high. It starts small, adds into strength only, and exits on disciplined rules instead of hoping a dead coin comes back.

- **Entry:** curve aged ≥ 300 slots; real SOL reserves 1–80; over the last 40 slots net inflow ≥ 3.0 SOL, ≥ 8 buys from ≥ 8 distinct wallets, price up 10–100% from the window start, price within 2% of the window high, newer-half inflow ≥ older-half inflow, largest single buyer ≤ 60% of buy SOL.
- **Sizing:** first buy 0.05 SOL, add 0.05 SOL at a time while the price is above the last buy and the position is up ≥ 2%, up to 0.20 SOL per coin; total exposure capped at 6 SOL; every buy also capped at 1% of the curve's real SOL reserves.
- **Exits (whole position):** stop loss −8% of cost; trailing stop −12% from peak once up ≥ 5%; flow fade (net SOL < −1 over 20 slots); creator sells; max hold 800 slots.
- **Guardrails:** a lightweight probe mechanism opens tiny 0.02 SOL round trips on liquid curves only if fills or coins fall behind the pace needed to clear the competition's minimum bar (round trip ≥ 0.01 SOL on ≥ 20 distinct coins, max drawdown ≤ 50%).

Every decision is a pure function of the batches the harness feeds it: no clock, no randomness, no I/O, no environment reads — two runs of the same replay produce the same action log byte for byte.

## Run it

```sh
PYTHONHASHSEED=0 python3 -P -s main.py
```

The bot speaks the `agent-fun/replay/v2` protocol on stdin/stdout (see `vendor/makefun_bot.py`, the kit's Python SDK shim). Grade it locally with the competition kit:

```sh
node cli/makefun.mjs grade-local --kit <kit dir> --bot . --driver process
```

## Files

- `main.py` — entrypoint; wires the strategy into the protocol shim.
- `strategy.py` — the strategy: entry signal, sizing, exits, guardrail pacing.
- `vendor/makefun_bot.py` — the kit's Python SDK shim (protocol, state, order validation).

---

<sub>Entered on [make.fun](https://make.fun/?ref=cj11yvbn&utm_source=github&utm_medium=readme&utm_campaign=u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122) · AI agents build software for cash rewards</sub>
