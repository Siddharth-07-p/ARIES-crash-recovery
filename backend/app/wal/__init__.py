"""Write-Ahead Logging (WAL) subsystem module."""

from app.wal.constants import (
    DEFAULT_WAL_DIR,
    DEFAULT_WAL_FILENAME,
    DEFAULT_WAL_PATH,
    WAL_FORMAT_VERSION,
    WAL_RECORD_MAGIC,
)
from app.wal.exceptions import (
    InvalidLogRecordError,
    InvalidLSNError,
    LogCorruptionError,
    LogManagerClosedError,
    LogManagerError,
    TransactionLoggingError,
    WALFlushError,
)
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecord, LogRecordType

__all__ = [
    "DEFAULT_WAL_DIR",
    "DEFAULT_WAL_FILENAME",
    "DEFAULT_WAL_PATH",
    "WAL_FORMAT_VERSION",
    "WAL_RECORD_MAGIC",
    "LogManagerError",
    "LogCorruptionError",
    "InvalidLSNError",
    "InvalidLogRecordError",
    "WALFlushError",
    "LogManagerClosedError",
    "TransactionLoggingError",
    "LogRecordType",
    "LogRecord",
    "LogManager",
]
