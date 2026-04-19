"""
Backtest runner script (invoked by bot_manager's /run-backtest endpoint).

Takes a single CLI arg: JSON payload with name, strategy, tickers,
start_date, end_date, config. Calls backtest_engine.engine.run_backtest
and exits 0 on success / 1 on failure.

Runs in its own process so the sidecar can spawn multiple backtests
and the engine has its own clean interpreter (avoids SignalEngine
state bleeding between runs).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import date


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("run_backtest_engine")

    if len(argv) < 2:
        print("usage: python run_backtest_engine.py '{\"tickers\":[\"QQQ\"],\"start_date\":\"...\"}'",
              file=sys.stderr)
        return 2

    try:
        req = json.loads(argv[1])
    except json.JSONDecodeError as e:
        print(f"bad payload: {e}", file=sys.stderr)
        return 2

    # Resolve strategy name → strategy_id + class_path
    strategy_name = req.get("strategy", "ict")
    try:
        from db.connection import get_session
        from sqlalchemy import text
        session = get_session()
        row = session.execute(
            text("SELECT strategy_id, class_path FROM strategies "
                 "WHERE name = :n AND enabled = TRUE"),
            {"n": strategy_name},
        ).fetchone()
        session.close()
    except Exception as e:
        print(f"strategy lookup failed: {e}", file=sys.stderr)
        return 1
    if row is None:
        print(f"strategy '{strategy_name}' not found or disabled", file=sys.stderr)
        return 1
    strategy_id = int(row[0])

    # Import the engine + run
    from backtest_engine.engine import run_backtest

    try:
        result = run_backtest(
            tickers=list(req["tickers"]),
            start_date=_parse_date(req["start_date"]),
            end_date=_parse_date(req["end_date"]),
            strategy_id=strategy_id,
            config=req.get("config", {}),
            run_name=req.get("name"),
            progress_cb=lambda msg: log.info(msg),
        )
        log.info(f"Backtest {result['run_id']} complete — "
                 f"{result['trade_count']} trades")
        return 0
    except Exception as e:
        log.exception(f"Backtest failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
