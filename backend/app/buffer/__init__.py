"""Buffer management package for ARIES database engine."""

from app.buffer.buffer_manager import BufferManager
from app.buffer.constants import DEFAULT_BUFFER_POOL_SIZE
from app.buffer.exceptions import (
    BufferManagerClosedError,
    BufferManagerError,
    BufferPoolFullError,
    NoEvictableFrameError,
    PageNotResidentError,
    PagePinnedError,
)
from app.buffer.frame import Frame
from app.buffer.replacer import LRUReplacer

__all__ = [
    "BufferManager",
    "Frame",
    "LRUReplacer",
    "DEFAULT_BUFFER_POOL_SIZE",
    "BufferManagerError",
    "BufferPoolFullError",
    "NoEvictableFrameError",
    "PageNotResidentError",
    "PagePinnedError",
    "BufferManagerClosedError",
]
