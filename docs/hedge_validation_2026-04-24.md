# Hedge Validation Backtest — 2026-04-24

**Purpose:** Validate the design split between Class A (defined-risk, no hedge) and Class B (delta-hedged) strategies for the multi-leg completion plan (`docs/multi_leg_completion_plan.md`). If hedging materially helps drawdown / Sharpe, the live Phase 3 `EnvelopeExitMonitor` work is justified. If not, simpler Class A bracket-based exits suffice.

## Setup
- **Window:** 2026-02-25 → 2026-04-24 (58 days, yfinance limit)
- **Tickers:** AAPL, MSFT, NVDA, META, AMZN
- **Engine:** `backtest_engine/dn_variants_engine.py` extended with delta-hedge sim (ENH-066)
- **Hedge math:** mirrors live `strategy/delta_hedger.py` — share-equivalent net delta, signed, rebalance when `|residual| > band_shares`
- **Bars:** 5-min (rebalance every bar — proxy for the live 30-second loop)

## Results

| Variant | Mode | Trades | Win % | Total P&L | Max Loss | Hedge P&L | Rebals |
|---|---|---:|---:|---:|---:|---:|---:|
| **V5_HEDGED** (45-DTE, 16Δ/5Δ) | HEDGED | 19 | 52.6% | **+$7,326** | -$633 | +$1,107 | 14 |
| V5_HEDGED | unhedged | 19 | 57.9% | +$6,220 | -$1,176 | $0 | 0 |
| **V5B_SWEEP_WINNER** (25Δ/3Δ, IVR≥50) | hedged | 26 | 80.8% | +$53,336 | -$2,672 | -$1,965 | 55 |
| V5B_SWEEP_WINNER | UNHEDGED | 26 | 88.5% | **+$55,301** | -$1,702 | $0 | 0 |
| **ZDN_WEEKLY** butterfly | HEDGED | 13 | 69.2% | **+$2,372** | -$62 | +$780 | 243 |
| ZDN_WEEKLY | unhedged | 13 | 69.2% | +$1,593 | -$178 | $0 | 0 |

## Reading

### V5_HEDGED — keep hedging (Class B)
Hedging cost 5pts of win-rate but cut max-loss in half (-$633 vs -$1,176) and added net +$1,107 from gamma scalping. **Drawdown protection is real.** Stays Class B.

### V5B_SWEEP_WINNER — drop hedging (now Class A)
Hedging *reduced* total P&L by $1,965 over 26 trades. The IVR≥50 entry filter already only enters when premium is rich enough that the 25Δ/3Δ structure has inherent gamma protection — additional stock hedging adds rebalance friction without improving outcomes. **Reclassified as Class A.** Updated `delta_hedge=False` in the variant config.

### ZDN_WEEKLY butterfly — hedging IS the strategy (Class B)
Same win-rate but **+50% total P&L** (+$2,372 vs +$1,593) and ⅓ max-loss with hedging. 243 rebalances over 13 trades = ~19/trade — this is gamma-scalping by design. The narrow-band hedge captures intraday vol while the butterfly's theta works. **Confirms the ZDN design hypothesis.** Stays Class B.

## Implications for the live work

- Phase 2 (Class A envelope brackets) covers: V1, V2, V3, V4, **V5b** (newly).
- Phase 3 (bot-managed exits) covers: V5_HEDGED + all four ZDN variants.
- Total Phase 3 strategies on the books today: 5. Smaller surface than initially scoped.

## Caveats
- 58 days is short. Long-history backtest (`scripts/run_dn_variant_backtest_longhistory.py`) should rerun with hedge sim before final go-live decision.
- Hedge sim rebalances on 5-min bars. Live runs every 30s; finer rebalancing should improve hedge P&L on Class B at the cost of more trades. ZDN's +$780 hedge profit may be a floor.
- BS pricing at flat 20% IV — real IV surface (especially the smile on OTM wings) would shift these numbers.
- No commissions / slippage modeled. ~250 rebalances/13 trades = $250+ in stock commissions IRL would erode ZDN's hedge profit. Worth re-running with a fill model.

## Next
1. Phase 3 live (`EnvelopeExitMonitor`) — same math as the sim.
2. Phase 2 live (Class A envelope brackets) — for V1-V4 + V5b.
3. Re-run hedge validation on 1-year history before re-enabling DN/ZDN strategies live.
