"""
Email Alerter — sends ICT signal alerts via Gmail.
Format per PDF spec:
  Subject: [ICT ALERT] QQQ LONG_iFVG Entry=573.45 SL=572.80 TP=576.20
  Body: full signal details
"""
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import pytz
import config

log = logging.getLogger(__name__)

PT  = pytz.timezone("America/Los_Angeles")
UTC = pytz.utc


def parse_strike_from_symbol(symbol: str) -> str:
    """
    Parse strike price from OCC option symbol.
    e.g. 'QQQ   250327C00573000' → '$573.00'
    """
    try:
        # Strike is last 8 digits, divide by 1000
        strike = int(symbol.strip()[-8:]) / 1000
        return f"${strike:.2f}"
    except Exception:
        return symbol


def send_signal_email(signal: dict, trade: dict = None):
    """
    Send an email alert when an ICT signal fires.
    """
    try:
        now_utc = datetime.now(UTC)
        now_pt  = now_utc.astimezone(PT)

        signal_type  = signal.get("signal_type", "UNKNOWN")
        entry        = signal.get("entry_price", 0)
        sl           = signal.get("sl", 0)
        tp           = signal.get("tp", 0)
        raid         = signal.get("raid", {})
        confirmation = signal.get("confirmation", {})

        # ── Subject ───────────────────────────────────────
        alert_tag = "[ICT ALERT ONLY]" if signal.get("alert_only") else "[ICT TRADE]"
        subject = (
            f"{alert_tag} {signal.get("ticker", "QQQ")} {signal_type} "
            f"Entry={entry:.2f} SL={sl:.2f} TP={tp:.2f}"
        )

        # ── Body ──────────────────────────────────────────
        fvg = signal.get("fvg", {})
        ob  = signal.get("ob", {})
        direction  = signal.get("direction", "LONG")
        option_type = "Put" if direction == "SHORT" else "Call"

        alert_only = signal.get("alert_only", False)
        body_lines = [
            "=" * 50,
            f"ICT SIGNAL ALERT — {signal.get("ticker", "QQQ")}",
            "=" * 50,
            f"⚠️  ALERT ONLY — No trade placed (outside trade window)" if alert_only else "✅  TRADE OPENED",
            "",
            f"Signal Type:  {signal_type}",
            f"Direction:    {direction}",
            f"Time (PT):    {now_pt.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Time (UTC):   {now_utc.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "── Entry ───────────────────────────────",
            f"Entry Price:  ${entry:.2f}  ({signal.get('ticker', 'QQQ')} level)",
            f"Stop Loss:    ${sl:.2f}",
            f"Take Profit:  ${tp:.2f}",
            f"Risk/Reward:  {((tp - entry) / (entry - sl)):.1f}R" if entry > sl else "",
            "",
            "── Option Details ──────────────────────",
            f"Option:       {signal.get('ticker', 'QQQ')} {option_type} (0DTE ATM)",
            f"Strike:       {parse_strike_from_symbol(trade.get('symbol', '')) if trade else 'Pending'}",
            f"Premium Paid: ${trade.get('entry_price', 0):.2f} per contract" if trade else "Premium: Pending",
            f"Contracts:    {trade.get('contracts', config.CONTRACTS) if trade else config.CONTRACTS}",
            f"Total Cost:   ${trade.get('entry_price', 0) * 100 * config.CONTRACTS:.2f}" if trade else "",
            "",
            "── Raid Details ────────────────────────",
            f"Raided Level: {raid.get('raided_level', 'N/A')} @ ${raid.get('raided_price', 0):.2f}",
            f"Raid Low:     ${raid.get('raid_low', 0):.2f}",
            f"Raid Time:    {raid.get('bar_time', 'N/A')}",
            "",
            "── Confirmation ────────────────────────",
            f"Disp. Candle: {confirmation.get('disp_time', 'N/A')}",
            f"Disp. Close:  ${confirmation.get('disp_close', 0):.2f}",
            "",
        ]

        # Add FVG details for Signal A (long or short)
        if "iFVG" in signal_type and fvg:
            body_lines += [
                "── Fair Value Gap (iFVG) ───────────────",
                f"FVG Lower:    ${fvg.get('fvg_lower', 0):.2f}",
                f"FVG Upper:    ${fvg.get('fvg_upper', 0):.2f}",
                f"FVG Mid:      ${fvg.get('fvg_mid', 0):.2f}",
                f"FVG Size:     ${fvg.get('fvg_size', 0):.2f}",
                "",
            ]

        # Add OB details for Signal B (long or short)
        if "OB" in signal_type and ob:
            body_lines += [
                "── Order Block (OB) ────────────────────",
                f"OB Low:       ${ob.get('ob_low', 0):.2f}",
                f"OB High:      ${ob.get('ob_high', 0):.2f}",
                "",
            ]

        body_lines += [
            "── Bot Settings ────────────────────────",
            f"Mode:         {'DRY RUN' if config.DRY_RUN else 'LIVE'}",
            f"Contracts:    {trade.get('contracts', config.CONTRACTS) if trade else config.CONTRACTS}x {signal.get('ticker', 'QQQ')} ATM 0DTE {option_type}",
            f"Option TP:    {config.PROFIT_TARGET:.0%}",
            f"Option SL:    {config.STOP_LOSS:.0%}",
            f"Setup ID:     {signal.get('setup_id', 'N/A')}",
            "=" * 50,
        ]

        body = "\n".join(body_lines)

        # ── Send email ────────────────────────────────────
        msg = MIMEMultipart()
        msg["From"]    = config.EMAIL_FROM
        msg["To"]      = config.EMAIL_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.EMAIL_FROM, config.EMAIL_APP_PASSWORD)
            server.send_message(msg)

        log.info(f"Email alert sent to {config.EMAIL_TO}: {subject}")

    except Exception as e:
        log.error(f"Failed to send email alert: {e}")


def send_trade_result_email(trade: dict, result: str, exit_price: float):
    """
    Send an email when a trade closes (win or loss).
    """
    try:
        entry     = trade.get("entry_price", 0)
        pnl_pct   = (exit_price - entry) / entry * 100 if entry else 0
        pnl_usd   = (exit_price - entry) * 100 * trade.get("contracts", 1)
        symbol    = trade.get("symbol", "N/A")

        subject = (
            f"[ICT TRADE {'WIN' if result == 'WIN' else 'LOSS'}] "
            f"{symbol} {pnl_pct:+.1f}% (${pnl_usd:+.0f})"
        )

        body = "\n".join([
            "=" * 50,
            f"TRADE CLOSED — {result}",
            "=" * 50,
            f"Symbol:    {symbol}",
            f"Contracts: {trade.get('contracts', 0)}",
            f"Entry:     ${entry:.2f}",
            f"Exit:      ${exit_price:.2f}",
            f"P&L:       {pnl_pct:+.1f}%  (${pnl_usd:+.0f})",
            f"Mode:      {'DRY RUN' if config.DRY_RUN else 'LIVE'}",
            "=" * 50,
        ])

        msg = MIMEMultipart()
        msg["From"]    = config.EMAIL_FROM
        msg["To"]      = config.EMAIL_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.EMAIL_FROM, config.EMAIL_APP_PASSWORD)
            server.send_message(msg)

        log.info(f"Trade result email sent: {subject}")

    except Exception as e:
        log.error(f"Failed to send trade result email: {e}")
