"""Custom exceptions for the page storage layer."""


class StorageError(Exception):
    """Base exception for all storage layer errors."""
    pass


class InvalidPageIdError(StorageError):
    """Raised when a page ID is invalid (e.g., negative, non-integer, or mismatched)."""
    pass


class InvalidPageSizeError(StorageError):
    """Raised when page data does not match the expected PAGE_SIZE."""
    pass


class PageOutOfBoundsError(StorageError):
    """Raised when reading or writing outside the valid boundaries of a page."""
    pass


class PageNotFoundError(StorageError):
    """Raised when accessing a page ID that has not been allocated or does not exist."""
    pass


class CorruptedDatabaseFileError(StorageError):
    """Raised when a database file is malformed or its size is not a multiple of PAGE_SIZE."""
    pass


class DiskManagerClosedError(StorageError):
    """Raised when an operation is attempted on a closed DiskManager."""
    pass
