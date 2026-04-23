# DN Variant Backtest Report — 2026-04-23

**Window**: 2026-02-24 → 2026-04-23 (58 days, yfinance 5m bars)
**Universe**: 18 tickers across 4 tiers
**Variants**: v1_baseline, v2_hold_day, v3_phaseB, v4_filtered, v5_hedged
**VIX data**: ✅ present

## Executive summary by variant

| Variant | Trades | Net P&L $ | Avg Trade $ | Win Rate | Max DD $ | PF | Hold Days |
|---|---|---|---|---|---|---|---|
| v1_baseline | 56 | +10204 | +182.2 | 75% | -728 | 20.47 | 19.3 |
| v2_hold_day | 216 | -29054 | -134.5 | 37% | -4202 | 0.01 | 0.9 |
| v3_phaseB | 73 | -12129 | -166.2 | 42% | -2196 | 0.15 | 11.7 |
| v4_filtered | 71 | +6751 | +95.1 | 73% | -2038 | 2.46 | 9.6 |
| v5_hedged | 71 | +16216 | +228.4 | 73% | -2865 | 3.29 | 9.6 |

## Per-tier, per-variant detail

### Tier 0 — SPY, QQQ, IWM

| Ticker | V1 base | V2 hold-day | V3 phaseB | V4 filtered | V5 hedged |
|---|---|---|---|---|---|
| SPY | +647 (4t/75%) | -2021 (12t/42%) | -1565 (4t/25%) | +336 (4t/75%) | -364 (4t/75%) |
| QQQ | +740 (4t/75%) | -4031 (12t/8%) | -1297 (3t/0%) | +1631 (3t/67%) | +4323 (3t/67%) |
| IWM | +88 (2t/50%) | +369 (12t/67%) | -374 (4t/50%) | +71 (4t/75%) | -687 (4t/75%) |

### Tier 1 — AAPL, MSFT, NVDA, AMZN, GOOGL, META

| Ticker | V1 base | V2 hold-day | V3 phaseB | V4 filtered | V5 hedged |
|---|---|---|---|---|---|
| AAPL | +255 (2t/50%) | -159 (12t/58%) | -466 (3t/0%) | -179 (3t/33%) | -545 (3t/33%) |
| MSFT | +1457 (4t/100%) | -2190 (12t/42%) | -1437 (4t/25%) | -1433 (4t/25%) | -934 (4t/25%) |
| NVDA | +523 (2t/100%) | -1167 (12t/42%) | -953 (3t/0%) | -711 (2t/0%) | -1470 (2t/0%) |
| AMZN | +123 (3t/67%) | -823 (12t/50%) | +869 (6t/83%) | +211 (4t/75%) | +1260 (4t/75%) |
| GOOGL | +627 (4t/75%) | -1406 (12t/42%) | +448 (4t/75%) | +606 (3t/100%) | +913 (3t/100%) |
| META | +756 (3t/67%) | -2797 (12t/17%) | -1872 (5t/40%) | -1276 (3t/33%) | -2158 (3t/33%) |

### Tier 2 — TSLA, AMD, AVGO, COIN

| Ticker | V1 base | V2 hold-day | V3 phaseB | V4 filtered | V5 hedged |
|---|---|---|---|---|---|
| TSLA | +1200 (4t/75%) | -2900 (12t/17%) | -517 (3t/33%) | -116 (3t/33%) | +705 (3t/33%) |
| AMD | +363 (2t/50%) | -1804 (12t/25%) | -1289 (4t/25%) | +1930 (6t/83%) | +5672 (6t/83%) |
| AVGO | +559 (3t/67%) | -1400 (12t/42%) | -1079 (4t/25%) | -821 (3t/67%) | -821 (3t/67%) |
| COIN | +643 (3t/67%) | -3414 (12t/8%) | -847 (5t/40%) | +2222 (6t/100%) | +3360 (6t/100%) |

### Tier 3 — MSTR, DELL, INTC, PLTR, MU

| Ticker | V1 base | V2 hold-day | V3 phaseB | V4 filtered | V5 hedged |
|---|---|---|---|---|---|
| MSTR | +1076 (4t/100%) | -1404 (12t/58%) | +516 (5t/80%) | +2709 (8t/100%) | +4940 (8t/100%) |
| DELL | -524 (1t/0%) | -407 (12t/33%) | -1820 (4t/0%) | +747 (3t/67%) | -111 (3t/67%) |
| INTC | +13 (6t/83%) | -395 (12t/50%) | +20 (5t/80%) | +444 (5t/100%) | +752 (5t/100%) |
| PLTR | +514 (2t/100%) | -1714 (12t/33%) | -735 (3t/33%) | +456 (3t/100%) | +836 (3t/100%) |
| MU | +1145 (3t/67%) | -1392 (12t/33%) | +270 (4t/75%) | -75 (4t/75%) | +545 (4t/75%) |

## Interpretation notes

- **Cells show** `net_pnl_$ (trades/win_rate)`.
- 60-day window is short for statistical significance;
  use for directional signal, not absolute profit expectations.
- V3-V5 target 45 DTE → fewer completed trades in a 60-day window.
- V5 hedged approximates delta-hedge cost via BS pricing only;
  slippage + commissions NOT included — treat as upper-bound.
- Compare V1→V3 to isolate Phase-B entry construction effect.
- Compare V3→V4 to see filter value-add.
- Compare V4→V5 to see Phase-C risk-management value-add.

## Raw data

- Per-(variant, ticker) metrics: `data/backtest_results_2026-04-23.csv`
- Individual trades: `data/backtest_trades_2026-04-23.csv`
- Variant configs: `strategy/delta_neutral_variants.py`
- Decisions log: `docs/dn_variant_decisions.md`
