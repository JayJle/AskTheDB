"""Project-wide logging configuration.

Usage:
    from sql_graph.log_utils import log
    log.info('Something happened')
    log.exception(error)  # Inside an except block: logs the message and the full traceback

Logs are written to the console and to a rotating file in ``<project root>/logs``.
Set the ``LOG_LEVEL`` environment variable (e.g. ``INFO``) to change the minimum level;
the default is ``DEBUG``. Levels: TRACE < DEBUG < INFO < SUCCESS < WARNING < ERROR < CRITICAL.
"""
import os
import sys
from pathlib import Path

from loguru import logger

LOG_DIR = Path(__file__).resolve().parent.parent / 'logs'
LOG_FILE = LOG_DIR / 'askthedb.log'
LOG_LEVEL = os.getenv('LOG_LEVEL', 'DEBUG').upper()

CONSOLE_FORMAT = (
    '<green>{time:YYYY-MM-DD HH:mm:ss}</green> | '
    '<level>{level: <8}</level> | '
    '{process.name} | {thread.name} | '
    '<cyan>{module}</cyan>.<cyan>{function}</cyan>:<cyan>{line}</cyan> - '
    '<level>{message}</level>'
)
FILE_FORMAT = (
    '{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {process.name} | {thread.name} | '
    '{module}.{function}:{line} - {message}'
)


def setup_logger():
    """Configure loguru's global logger with a console sink and a rotating file sink."""
    LOG_DIR.mkdir(exist_ok=True)
    logger.remove()  # Remove loguru's default handler so messages are not printed twice
    logger.add(sys.stdout, level=LOG_LEVEL, format=CONSOLE_FORMAT)
    logger.add(
        LOG_FILE,
        level=LOG_LEVEL,
        format=FILE_FORMAT,
        encoding='utf-8',
        rotation='10 MB',  # Start a new file when the current one reaches 10 MB
        retention=20,  # Keep at most 20 rotated files
    )
    return logger


log = setup_logger()
