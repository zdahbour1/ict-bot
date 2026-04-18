"""
Backtest Framework (ENH-019).

Simulates the live trading strategy against historical data by reusing
strategy/signal_engine.py and strategy/exit_conditions.py — the same
code paths that run in production. See docs/backtest_framework.md.

Modules:
- engine.py         — bar-by-bar simulation loop
- fill_model.py     — simulated order fill at bar price
- metrics.py        — summary metrics (win rate, P&L, Sharpe, drawdown)
- data_provider.py  — historical data loader (yfinance or CSV)
"""
