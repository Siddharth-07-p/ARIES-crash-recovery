"""Frame structure holding page metadata and in-memory buffer state."""

from typing import Optional
from app.storage.page import Page


class Frame:
    """Represents a single buffer frame in the buffer pool.

    Attributes:
        frame_id: Fixed index of the frame within the buffer pool.
        page_id: ID of the database page currently resident in this frame (None if empty).
        page: Page object currently held in this frame (None if empty).
        pin_count: Number of active references/pins holding this page in memory.
        is_dirty: Whether the resident page was modified in memory and differs from disk.
    """

    __slots__ = ("_frame_id", "_page_id", "_page", "_pin_count", "_is_dirty")

    def __init__(self, frame_id: int) -> None:
        """Initialize an empty buffer frame."""
        self._frame_id: int = frame_id
        self._page_id: Optional[int] = None
        self._page: Optional[Page] = None
        self._pin_count: int = 0
        self._is_dirty: bool = False

    @property
    def frame_id(self) -> int:
        """Return the physical index of this frame."""
        return self._frame_id

    @property
    def page_id(self) -> Optional[int]:
        """Return the resident page ID, or None if empty."""
        return self._page_id

    @property
    def page(self) -> Optional[Page]:
        """Return the resident Page object, or None if empty."""
        return self._page


    @property
    def pin_count(self) -> int:
        """Return the current pin count."""
        return self._pin_count

    @pin_count.setter
    def pin_count(self, count: int) -> None:
        """Set the pin count."""
        if count < 0:
            raise ValueError(f"pin_count cannot be negative, got {count}")
        self._pin_count = count

    @property
    def is_dirty(self) -> bool:
        """Return True if the page has unpersisted modifications."""
        return self._is_dirty

    @is_dirty.setter
    def is_dirty(self, dirty: bool) -> None:
        """Set the dirty flag."""
        self._is_dirty = bool(dirty)

    @property
    def is_empty(self) -> bool:
        """Return True if this frame does not contain a resident page."""
        return self._page_id is None

    @property
    def is_pinned(self) -> bool:
        """Return True if this frame has active pins and cannot be evicted."""
        return self._pin_count > 0

    def load_page(
        self,
        page_id: int,
        page: Page,
        pin_count: int = 1,
        is_dirty: bool = False,
    ) -> None:
        """Populate this frame with a new resident page.

        Args:
            page_id: ID of the loaded page.
            page: In-memory Page instance.
            pin_count: Initial pin count (default 1 for newly fetched pages).
            is_dirty: Initial dirty status (default False).
        """
        self._page_id = page_id
        self._page = page
        self._pin_count = max(0, pin_count)
        self._is_dirty = is_dirty

    def pin(self) -> int:
        """Increment pin count by 1.

        Returns:
            Updated pin count.
        """
        self._pin_count += 1
        return self._pin_count

    def unpin(self, is_dirty: bool = False) -> int:
        """Decrement pin count by 1 and optionally mark frame dirty.

        Args:
            is_dirty: If True, marks this frame as dirty.

        Returns:
            Updated pin count.

        Raises:
            ValueError: If pin count is already 0.
        """
        if self._pin_count <= 0:
            raise ValueError(
                f"Frame {self._frame_id} (page {self._page_id}) is already unpinned (pin_count=0)"
            )
        self._pin_count -= 1
        if is_dirty:
            self._is_dirty = True
        return self._pin_count

    def clear(self) -> None:
        """Reset frame to empty state."""
        self._page_id = None
        self._page = None
        self._pin_count = 0
        self._is_dirty = False

    def __repr__(self) -> str:
        """String representation of the buffer frame."""
        return (
            f"Frame(id={self._frame_id}, page_id={self._page_id}, "
            f"pin_count={self._pin_count}, dirty={self._is_dirty})"
        )
