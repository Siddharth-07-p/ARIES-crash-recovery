"""Custom exceptions for the buffer manager layer."""

from app.storage.exceptions import StorageError


class BufferManagerError(StorageError):
    """Base exception for buffer manager errors."""
    pass


class BufferPoolFullError(BufferManagerError):
    """Raised when buffer pool is full and no frames can be allocated."""
    pass


class NoEvictableFrameError(BufferPoolFullError):
    """Raised when all buffer frames are pinned and no victim can be evicted."""
    pass


class PageNotResidentError(BufferManagerError):
    """Raised when an operation is requested on a page that is not currently in the buffer pool."""
    pass


class PagePinnedError(BufferManagerError):
    """Raised when an illegal operation is attempted on a pinned buffer page."""
    pass


class BufferManagerClosedError(BufferManagerError):
    """Raised when operations are attempted on a closed BufferManager."""
    pass
