"""
Centralized error handling — ensures every error is captured, logged, and stored.
No error should ever be silently swallowed.
"""
import logging
import traceback as tb

log = logging.getLogger(__name__)


def handle_error(component: str, operation: str, error: Exception,
                 context: dict = None, critical: bool = False) -> None:
    """
    Standard error handler. Every caught exception in the codebase should
    call this instead of bare except/pass.

    Args:
        component: Which part of the system (e.g., "exit_manager", "scanner-QQQ")
        operation: What was being attempted (e.g., "update_trade_price", "place_order")
        error: The caught exception
        context: Additional context (trade_id, symbol, etc.)
        critical: If True, this is a serious error that needs immediate attention
    """
    error_type = type(error).__name__
    error_msg = str(error)
    trace = tb.format_exc()

    level = "error" if critical else "warn"
    log_msg = f"[{component}] {operation} failed: {error_type}: {error_msg}"

    if critical:
        log.error(log_msg)
    else:
        log.warning(log_msg)

    # Store in system_log table (general logging)
    try:
        from db.writer import add_system_log
        details = context.copy() if context else {}
        details["error_type"] = error_type
        details["traceback"] = trace[:2000]
        add_system_log(component, level, f"{operation}: {error_msg}"[:500], details)
    except Exception:
        # If we can't even log to DB, at least it's in the Python log
        pass

    # Also store in errors table (for dashboard error popup per thread/ticker)
    try:
        from db.writer import log_error
        # Extract ticker from component name (e.g., "scanner-QQQ" → "QQQ")
        ticker = None
        if "-" in component:
            parts = component.split("-", 1)
            ticker = parts[1] if len(parts) > 1 else None
        log_error(
            thread_name=component,
            ticker=ticker,
            trade_id=(context or {}).get("trade_id"),
            error_type=f"{operation}:{error_type}",
            message=f"{operation}: {error_msg}"[:2000],
            trace=trace[:5000] if trace else None,
        )
    except Exception:
        pass


def safe_call(func, *args, component: str = "unknown", operation: str = "unknown",
              default=None, critical: bool = False, context: dict = None, **kwargs):
    """
    Safely call a function, catching and logging any errors.
    Returns the function result or `default` on failure.

    Usage:
        result = safe_call(client.get_option_price, symbol,
                          component="exit_manager", operation="get_price",
                          default=None, context={"symbol": symbol})
    """
    try:
        return func(*args, **kwargs)
    except Exception as e:
        handle_error(component, operation, e, context=context, critical=critical)
        return default
