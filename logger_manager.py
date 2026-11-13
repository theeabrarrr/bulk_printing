"""
logger_manager.py
Enterprise-Grade Centralized Logging System for the Oracle ERP Automation Suite.
Provides dual-channel logging:
  1. RotatingFileHandler ('logs/app.log', 5 MB max size, 5 backups, UTF-8, DEBUG+ level)
     with full timestamps, module/function/line metadata, and exception stack traces.
  2. StreamHandler (sys.stdout, INFO+ level) with clean, operator-friendly formatting
     so interactive CLI menus and live status updates remain crisp.
  3. Global sys.excepthook integration to capture any unhandled fatal crashes.
"""

import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from typing import Dict, Optional

import config_manager

ROOT_LOGGER_NAME = "erp_automation"
_LOGGING_INITIALIZED = False
_ACTIVE_LOG_FILE_PATH: Optional[str] = None
_ORIG_EXCEPTHOOK = sys.excepthook


def _resolve_log_level(level_name: str, default: int = logging.DEBUG) -> int:
    """Resolves a string level name (e.g., 'DEBUG', 'INFO') to a logging constant."""
    if isinstance(level_name, int):
        return level_name
    if not level_name:
        return default
    resolved = getattr(logging, str(level_name).strip().upper(), None)
    return resolved if isinstance(resolved, int) else default


def _install_exception_hook() -> None:
    """
    Installs a global sys.excepthook that records any uncaught exception
    with full stack trace into the rotating log file before delegating to the
    original excepthook.
    """
    def _handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            logger = logging.getLogger(ROOT_LOGGER_NAME)
            logger.debug("Application interrupted via KeyboardInterrupt (Ctrl+C).")
            _ORIG_EXCEPTHOOK(exc_type, exc_value, exc_traceback)
            return

        logger = logging.getLogger(ROOT_LOGGER_NAME)
        logger.critical(
            "Uncaught fatal exception terminated execution:",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        _ORIG_EXCEPTHOOK(exc_type, exc_value, exc_traceback)

    sys.excepthook = _handle_exception


def close_logging_handlers() -> None:
    """Closes and detaches all handlers from the root application logger."""
    global _LOGGING_INITIALIZED, _ACTIVE_LOG_FILE_PATH
    base_logger = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(base_logger.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
        base_logger.removeHandler(handler)
    _LOGGING_INITIALIZED = False
    _ACTIVE_LOG_FILE_PATH = None


def setup_logging(
    config: Optional[Dict] = None,
    force_reinit: bool = False,
    enable_console: bool = True,
) -> logging.Logger:
    """
    Initializes or reconfigures the centralized application logger.

    Args:
        config: Configuration dictionary. Loaded via config_manager if None.
        force_reinit: If True, tears down existing handlers and re-initializes.
        enable_console: If True, attaches the INFO+ StreamHandler to sys.stdout.

    Returns:
        Configured base logging.Logger ('erp_automation').
    """
    global _LOGGING_INITIALIZED, _ACTIVE_LOG_FILE_PATH

    log_cfg = config_manager.get_logging_settings(config)
    log_dir = config_manager.resolve_path(log_cfg.get("log_dir", "logs"))
    log_file_name = log_cfg.get("log_file", "app.log")
    log_file_path = os.path.abspath(os.path.join(log_dir, log_file_name))

    max_bytes = log_cfg.get("max_bytes", 5242880)
    backup_count = log_cfg.get("backup_count", 5)
    file_level = _resolve_log_level(log_cfg.get("file_level", "DEBUG"), logging.DEBUG)
    console_level = _resolve_log_level(log_cfg.get("console_level", "INFO"), logging.INFO)

    base_logger = logging.getLogger(ROOT_LOGGER_NAME)

    if (
        _LOGGING_INITIALIZED
        and not force_reinit
        and _ACTIVE_LOG_FILE_PATH == log_file_path
        and base_logger.handlers
    ):
        return base_logger

    close_logging_handlers()

    base_logger.setLevel(logging.DEBUG)
    base_logger.propagate = False

    # 1. Configure RotatingFileHandler (DEBUG+ with full technical context)
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file_path,
            mode="a",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
        file_handler.setLevel(file_level)
        file_formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        base_logger.addHandler(file_handler)
        _ACTIVE_LOG_FILE_PATH = log_file_path
    except Exception as e:
        sys.stderr.write(f"[!] Warning: Could not initialize log file at '{log_file_path}': {e}\n")

    # 2. Configure Console StreamHandler (INFO+ with clean operator-friendly output)
    if enable_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(console_level)
        console_formatter = logging.Formatter(fmt="%(message)s")
        console_handler.setFormatter(console_formatter)
        base_logger.addHandler(console_handler)

    _install_exception_hook()
    _LOGGING_INITIALIZED = True
    base_logger.debug(
        "Logging initialized (file='%s', max_bytes=%d, backups=%d, file_level=%s, console_level=%s).",
        log_file_path,
        max_bytes,
        backup_count,
        logging.getLevelName(file_level),
        logging.getLevelName(console_level),
    )
    return base_logger


def get_logger(module_name: Optional[str] = None) -> logging.Logger:
    """
    Returns a child logger under 'erp_automation.<module_name>'.
    Automatically initializes default logging if setup_logging() has not been called yet.
    """
    if not _LOGGING_INITIALIZED:
        setup_logging()

    if not module_name or module_name == ROOT_LOGGER_NAME:
        return logging.getLogger(ROOT_LOGGER_NAME)

    clean_name = str(module_name).strip()
    if clean_name.startswith(f"{ROOT_LOGGER_NAME}."):
        return logging.getLogger(clean_name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{clean_name}")


def get_active_log_file_path() -> Optional[str]:
    """Returns the absolute path of the currently active rotating log file."""
    return _ACTIVE_LOG_FILE_PATH
