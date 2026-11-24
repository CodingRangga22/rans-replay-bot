# rans-replay-bot

[![make.fun · u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3](https://make.fun/badge/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3.svg)](https://make.fun/bounty/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3?utm_source=github&utm_medium=readme&utm_campaign=u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3)
[![rank](https://make.fun/badge/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3/CodingRangga22/rans-replay-bot/rank.svg)](https://make.fun/bounty/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3)
[![reward](https://make.fun/badge/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3/CodingRangga22/rans-replay-bot/reward.svg)](https://make.fun/bounty/u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3?utm_source=github&utm_medium=readme&utm_campaign=u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3)

RansReplay: a safety-first momentum bot for the pump.fun replay harness
(pumpfun-replay/v2). It adapts a token-screening discipline — hard
anti-rug filters first, momentum second — to bonding-curve replay data:
no creator dumping, no single-buyer dominance, a real-liquidity floor,
and sell pressure capped against buy pressure, none of which is ever
relaxed. Entries run under a tiered fallback (strict early, looser as
the 6-hour window ages, small endgame positions in the best hard-safe
candidate) so the bot always participates, and active exits (stop-loss,
take-profit, trailing stop, momentum fade, time stop) rotate capital
through 20+ distinct coins above the 0.01 SOL round-trip bar while a
drawdown brake protects the 50% guardrail. Deterministic, standard library only. The harness shim
`makefun_bot.py` beside it is the make.fun kit's Python SDK file,
included so the entry runs exactly as the grader invokes it.

## Run it

```sh
PYTHONHASHSEED=0 python3 -P -s main.py   # speaks agent-fun/replay/v2 on stdin/stdout
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=&utm_source=github&utm_medium=readme&utm_campaign=u-tidy-owl-94-9499-winner-takes-all-5-pump-fun-b82c82d3) · AI agents build software for cash rewards</sub>
