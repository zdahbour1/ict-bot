"""
Backtest Framework (ENH-019 + ENH-024).

Simulates the live trading strategy against historical data by reusing
strategy/signal_engine.py and strategy/exit_conditions.py — the same
code paths that run in production. Runs one strategy at a time per
active_strategy_design.md.

Separate package name `backtest_engine` (not `backtest`) because the
repo already has a legacy `backtest/` folder full of one-off research
scripts we don't want to disturb.

Modules:
- metrics.py             — summary stats (win rate, drawdown, Sharpe, PF)
- fill_model.py          — simulated order fills + slippage + commission
- indicators.py          — per-trade enrichment for data-science analysis
- data_provider.py       — historical 1m bar loader (yfinance + cache)
- engine.py              — bar-by-bar simulation loop
- writer.py              — DB writer for backtest_runs / backtest_trades
"""
