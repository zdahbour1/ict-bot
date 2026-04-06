"""
Webhook Server
Listens for POST requests from TradingView alerts.
When a bullish ICT signal arrives, it triggers the trade entry.

TradingView alert message format (paste this in your TradingView alert):
{
  "secret": "ict-secret-token",
  "signal": "bullish_sweep"
}
"""
import logging
from flask import Flask, request, jsonify
from strategy.exit_manager import ExitManager
from strategy.option_selector import select_and_enter, select_and_enter_put
import config

log = logging.getLogger(__name__)


def create_app(client, exit_manager: ExitManager) -> Flask:
    app = Flask(__name__)

    @app.route("/webhook", methods=["POST"])
    def webhook():
        data = request.get_json(silent=True) or {}

        # ── Security check ────────────────────────────────
        if data.get("secret") != config.WEBHOOK_SECRET:
            log.warning("Webhook received with wrong secret — ignored.")
            return jsonify({"status": "unauthorized"}), 401

        signal = data.get("signal", "").lower()
        log.info(f"Webhook received: signal='{signal}'")

        ticker = data.get("ticker", config.TICKERS[0])

        # ── Bullish ICT signal → Buy call ─────────────────
        if signal == "bullish_sweep":
            trade = select_and_enter(client, ticker)
            if trade:
                exit_manager.add_trade(trade)
                return jsonify({"status": "trade_opened", "symbol": trade["symbol"], "ticker": ticker}), 200
            else:
                return jsonify({"status": "skipped", "reason": "outside trading window"}), 200

        # ── Bearish ICT signal → Buy put ──────────────────
        if signal == "bearish_sweep":
            trade = select_and_enter_put(client, ticker)
            if trade:
                exit_manager.add_trade(trade)
                return jsonify({"status": "trade_opened", "symbol": trade["symbol"], "ticker": ticker}), 200
            else:
                return jsonify({"status": "skipped", "reason": "outside trading window"}), 200

        # ── Unknown signal ────────────────────────────────
        return jsonify({"status": "unknown_signal"}), 400

    @app.route("/status", methods=["GET"])
    def status():
        """Quick health check — visit in browser to confirm bot is running."""
        open_count = len(exit_manager.open_trades)
        return jsonify({
            "status":       "running",
            "paper_trading": config.PAPER_TRADING,
            "open_trades":  open_count,
        }), 200

    return app
