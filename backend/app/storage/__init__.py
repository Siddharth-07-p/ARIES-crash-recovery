"""Page-based persistent storage package for ARIES-inspired crash recovery engine."""

from app.storage.constants import DEFAULT_DB_PATH, PAGE_SIZE
from app.storage.disk_manager import DiskManager
from app.storage.exceptions import (
    CorruptedDatabaseFileError,
    DiskManagerClosedError,
    InvalidPageIdError,
    InvalidPageSizeError,
    PageNotFoundError,
    PageOutOfBoundsError,
    StorageError,
)
from app.storage.page import Page

__all__ = [
    "PAGE_SIZE",
    "DEFAULT_DB_PATH",
    "Page",
    "DiskManager",
    "StorageError",
    "InvalidPageIdError",
    "InvalidPageSizeError",
    "PageOutOfBoundsError",
    "PageNotFoundError",
    "CorruptedDatabaseFileError",
    "DiskManagerClosedError",
]
