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

    # Store in database
    try:
        from db.writer import add_system_log
        details = context or {}
        details["error_type"] = error_type
        details["traceback"] = trace[:2000]
        add_system_log(component, level, f"{operation}: {error_msg}"[:500], details)
    except Exception:
        # If we can't even log to DB, at least it's in the Python log
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
