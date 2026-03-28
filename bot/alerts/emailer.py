"""
Email alerter — sends ICT signal alerts via SMTP.
Supports a DRY_RUN mode (prints to console, no SMTP) for backtesting.
"""
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pytz
from loguru import logger

from bot import config
from bot.strategy.ict_long import Signal

PT = pytz.timezone("America/Los_Angeles")


def _format_body(sig: Signal) -> str:
    ts_utc = sig.bar_time.strftime("%Y-%m-%d %H:%M UTC")
    ts_pt  = sig.bar_time.tz_convert(PT).strftime("%Y-%m-%d %H:%M PT")

    lines = [
        f"=== ICT SCALP ALERT: {sig.signal_type} ===",
        "",
        f"Timestamp (UTC): {ts_utc}",
        f"Timestamp (PT):  {ts_pt}",
        f"Signal Type:     {sig.signal_type}",
        f"Signal ID:       {sig.signal_id}",
        "",
        f"--- Entry Details ---",
        f"Entry Price:     ${sig.entry:.4f}",
        f"Stop Loss:       ${sig.sl:.4f}  (below raid low {sig.raid_low:.4f} - buffer)",
        f"Take Profit:     ${sig.tp:.4f}  (nearest swing high)",
        f"Risk ($):        ${sig.entry - sig.sl:.4f}",
        f"Reward ($):      ${sig.tp - sig.entry:.4f}",
        f"R:R Ratio:       {((sig.tp - sig.entry) / max(sig.entry - sig.sl, 0.0001)):.2f}",
        "",
        f"--- Raid Info ---",
        f"Raided Level:    {sig.raided_level.name} @ ${sig.raided_level.price:.4f}",
        f"Raid Low Reached:{sig.raid_low:.4f}",
        f"Displacement Bar:{sig.displacement_time.strftime('%H:%M UTC')}",
        f"Displacement Ratio: {sig.displacement_ratio:.2f}x median body",
    ]

    if sig.fvg:
        lines += [
            "",
            f"--- iFVG Details ---",
            f"FVG Zone:        [{sig.fvg.lower:.4f} - {sig.fvg.upper:.4f}]",
            f"FVG Midpoint:    {sig.fvg.midpoint:.4f}",
            f"Close confirmed above midpoint.",
        ]

    if sig.ob:
        lines += [
            "",
            f"--- Order Block Details ---",
            f"OB Zone:         [{sig.ob.ob_low:.4f} - {sig.ob.ob_high:.4f}]",
            f"Price touched OB zone on this bar.",
        ]

    lines += [
        "",
        f"--- Reasoning ---",
        sig.reasoning,
        "",
        f"--- Config Snapshot ---",
        f"Symbol: {config.SYMBOL} | Window: {config.TRADE_WINDOW_START_PT}-{config.TRADE_WINDOW_END_PT} PT",
        f"RAID_THRESHOLD={config.RAID_THRESHOLD} | FVG_MIN_SIZE={config.FVG_MIN_SIZE}",
        f"DISPLACEMENT_BODY_MULT={config.DISPLACEMENT_BODY_MULT} | N_CONFIRM_BARS={config.N_CONFIRM_BARS}",
    ]
    return "\n".join(lines)


def send_alert(sig: Signal, dry_run: bool = False):
    """Send alert email. If dry_run=True, just log the message."""
    subject = (
        f"[ICT ALERT] {config.SYMBOL} {sig.signal_type} "
        f"Entry={sig.entry:.4f} SL={sig.sl:.4f} TP={sig.tp:.4f}"
    )
    body = _format_body(sig)

    if dry_run or not config.EMAIL_TO:
        logger.info(f"[DRY RUN] Would send email:\nSubject: {subject}\n{body}")
        return

    try:
        msg = MIMEMultipart()
        msg["From"]    = config.EMAIL_FROM
        msg["To"]      = config.EMAIL_TO
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(config.EMAIL_SMTP_HOST, config.EMAIL_SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(config.EMAIL_FROM, config.EMAIL_APP_PASSWORD)
            server.send_message(msg)

        logger.info(f"Alert email sent to {config.EMAIL_TO}: {subject}")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
