"""Exception hierarchy for the Write-Ahead Logging (WAL) subsystem."""


class LogManagerError(Exception):
    """Base exception for all LogManager and WAL errors."""


class LogCorruptionError(LogManagerError):
    """Raised when WAL record structure, header, magic, checksum, or payload is corrupt."""


class InvalidLSNError(LogManagerError):
    """Raised when an invalid, non-monotonic, or out-of-range LSN is specified."""


class InvalidLogRecordError(LogManagerError):
    """Raised when a log record contains invalid data fields, offsets, or images."""


class WALFlushError(LogManagerError):
    """Raised when flushing or syncing WAL data to disk fails."""


class LogManagerClosedError(LogManagerError):
    """Raised when operations are attempted on a closed LogManager instance."""


class TransactionLoggingError(LogManagerError):
    """Raised when an error occurs during transaction-specific logging operations."""
