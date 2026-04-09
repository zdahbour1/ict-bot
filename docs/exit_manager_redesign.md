# Exit Manager Redesign — Bracket Order Integration

## Date: 2026-04-09
## Status: Pending Implementation

## Core Principle
**The exit manager is the single authority for trade management.**
Bracket orders on IB are safety nets only — they protect positions if the bot crashes.
The exit manager always cancels brackets before taking action.

## Exit Flow (per trade, every 5 seconds)

1. Get current price (batch IB call)
2. Update P&L, peak, trailing stop
3. Update bracket SL on IB if trail changed
4. Check exit conditions (TP, SL, time, EOD, roll)
5. If exit triggered:
   a. **Cancel bracket orders** (TP + SL legs) on IB
   b. **Wait for cancellation** to complete
   c. **Check IB position** — verify contracts still exist
   d. If position still open → exit manager sends sell order
   e. If position already closed (bracket fired before cancel arrived) → just update DB
6. Update DB in all cases (current price, P&L, status)

## Startup Reconciliation (on bot start/restart)

Run once after IB connects, before scanners start:

1. **Get all IB option positions** (ib.positions())
2. **Get all DB open trades** (status='open')
3. **For each DB trade with no IB position:**
   - Bracket must have fired while bot was down
   - Check IB fills for exit price
   - Mark as closed in DB with "BRACKET (BOT OFFLINE)" reason
4. **For each IB position with no DB trade:**
   - Orphaned position — bot doesn't know about it
   - Insert into DB using IB position data (avgCost, qty, etc.)
   - Calculate TP/SL based on entry price
   - Check if bracket orders exist on IB for this position
   - If no brackets → create bracket orders (TP + SL)
   - Start monitoring as if bot opened it
5. **For each matched pair (DB + IB):**
   - Verify bracket orders exist on IB
   - If missing → create new bracket orders
   - Verify contract counts match
   - If IB qty < DB qty → partial close happened, update DB

## Key Rules

1. Exit manager NEVER sends a sell order without first cancelling brackets
2. After cancelling brackets, ALWAYS verify IB position before selling
3. All trade state changes written to DB immediately
4. Bracket orders are recreated on bot restart if missing
5. Reconciliation runs once on startup and periodically (every 5 min)

## Bug This Fixes

BUG-022: Double-sell creating negative positions
- Exit manager and bracket orders both selling the same contracts
- Result: short positions when should be flat
- Root cause: no coordination between exit manager and IB bracket execution
