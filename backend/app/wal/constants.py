"""Constants for the Write-Ahead Logging (WAL) subsystem."""

from pathlib import Path

# Default file paths
DEFAULT_WAL_DIR = "data"
DEFAULT_WAL_FILENAME = "database.wal"
DEFAULT_WAL_PATH = str(Path(DEFAULT_WAL_DIR) / DEFAULT_WAL_FILENAME)

# Binary format identifiers
WAL_RECORD_MAGIC = b"WALR"
WAL_FORMAT_VERSION = 1

# Record type codes for compact binary serialization
TYPE_CODE_BEGIN = 1
TYPE_CODE_COMMIT = 2
TYPE_CODE_ABORT = 3
TYPE_CODE_UPDATE = 4

# Minimum record size: 4 (magic) + 4 (len) + 8 (lsn) + 8 (prev_lsn) + 8 (tx_id) + 1 (type) + 4 (crc) = 37
MIN_RECORD_SIZE = 37
