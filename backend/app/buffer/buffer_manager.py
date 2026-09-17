"""BufferManager managing in-memory cached database pages above DiskManager."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union
from app.buffer.constants import DEFAULT_BUFFER_POOL_SIZE
from app.buffer.exceptions import (
    BufferManagerClosedError,
    BufferManagerError,
    NoEvictableFrameError,
    PageNotResidentError,
)
from app.buffer.frame import Frame
from app.buffer.replacer import LRUReplacer
from app.checkpoint.dirty_page_table import DirtyPageTable
from app.storage.disk_manager import DiskManager
from app.storage.exceptions import InvalidPageIdError
from app.storage.page import Page


class BufferManager:
    """Manages an in-memory pool of database page frames.

    Provides caching, pinning, dirty tracking, DPT maintenance, and deterministic LRU eviction
    for database pages backed by physical DiskManager I/O.
    """

    def __init__(
        self,
        disk_manager: DiskManager,
        pool_size: int = DEFAULT_BUFFER_POOL_SIZE,
        log_manager: Optional[Any] = None,
        dirty_page_table: Optional[DirtyPageTable] = None,
    ) -> None:
        """Initialize BufferManager with an underlying DiskManager, buffer pool size, LogManager, and DPT.

        Args:
            disk_manager: DiskManager instance for physical page I/O.
            pool_size: Maximum number of pages held in memory simultaneously.
            log_manager: Optional LogManager instance for WAL-before-flush enforcement.
            dirty_page_table: Optional DirtyPageTable instance (creates a new instance by default).

        Raises:
            ValueError: If pool_size is not a positive integer.
        """
        if not isinstance(pool_size, int) or isinstance(pool_size, bool) or pool_size <= 0:
            raise ValueError(f"pool_size must be a positive integer, got {pool_size!r}")

        self._disk_manager: DiskManager = disk_manager
        self._pool_size: int = pool_size
        self._log_manager: Optional[Any] = log_manager
        self._dirty_page_table: DirtyPageTable = (
            dirty_page_table if dirty_page_table is not None else DirtyPageTable()
        )
        self._frames: List[Frame] = [Frame(frame_id=i) for i in range(pool_size)]
        self._page_table: Dict[int, int] = {}  # page_id -> frame_id
        self._free_frames: List[int] = list(range(pool_size))
        self._replacer: LRUReplacer = LRUReplacer()
        self._is_closed: bool = False

    @property
    def dirty_page_table(self) -> DirtyPageTable:
        """Return the Dirty Page Table (DPT) associated with this BufferManager."""
        return self._dirty_page_table

    @property
    def dpt(self) -> DirtyPageTable:
        """Alias property for dirty_page_table."""
        return self._dirty_page_table

    @property
    def disk_manager(self) -> DiskManager:
        """Return the underlying DiskManager."""
        return self._disk_manager

    @property
    def log_manager(self) -> Optional[Any]:
        """Return the optional LogManager."""
        return self._log_manager

    @property
    def pool_size(self) -> int:
        """Return the buffer pool capacity."""
        return self._pool_size

    @property
    def is_closed(self) -> bool:
        """Check whether the BufferManager or underlying DiskManager is closed."""
        return self._is_closed or self._disk_manager.is_closed

    def _check_open(self) -> None:
        """Ensure BufferManager is open and ready for operations.

        Raises:
            BufferManagerClosedError: If the BufferManager is closed.
        """
        if self.is_closed:
            raise BufferManagerClosedError("BufferManager is closed. No operations permitted.")

    def _ensure_wal_flushed_for_frame(self, frame: Frame) -> None:
        """Ensure WAL records up to frame.page_lsn are durable before writing page to disk.

        Args:
            frame: Buffer frame containing page to be written.
        """
        if self._log_manager is not None and frame.page_lsn > 0:
            self._log_manager.flush_to_lsn(frame.page_lsn)

    def fetch_page(self, page_id: int) -> Page:
        """Fetch a page into the buffer pool and increment its pin count.

        If the page is already resident, reuses the resident frame and increments its pin count.
        If the page is not resident, reads it from disk into an available or evicted frame.

        Args:
            page_id: Target page ID to fetch.

        Returns:
            The in-memory Page instance.

        Raises:
            BufferManagerClosedError: If the manager is closed.
            InvalidPageIdError: If page_id is invalid.
            NoEvictableFrameError: If the buffer pool is full and all frames are pinned.
            PageNotFoundError: If page_id is not allocated on disk.
        """
        self._check_open()

        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
            raise InvalidPageIdError(
                f"Page ID must be a non-negative integer, got {page_id!r} (type={type(page_id).__name__})"
            )

        # 1. Cache Hit: Page is already resident in buffer pool
        if page_id in self._page_table:
            frame_id = self._page_table[page_id]
            frame = self._frames[frame_id]
            frame.pin()
            self._replacer.pin(frame_id)
            assert frame.page is not None
            return frame.page

        # 2. Cache Miss: Page must be read from disk
        # First verify disk existence and read raw page before mutating frame state
        page = self._disk_manager.read_page(page_id)

        # 3. Obtain an available frame
        if self._free_frames:
            frame_id = self._free_frames.pop(0)
            frame = self._frames[frame_id]
        else:
            # Buffer pool is full: find LRU victim among unpinned frames
            victim_frame_id = self._replacer.victim()
            if victim_frame_id is None:
                raise NoEvictableFrameError(
                    f"Buffer pool is full ({self._pool_size} frames) and all resident pages are pinned. Cannot evict."
                )

            frame_id = victim_frame_id
            frame = self._frames[frame_id]

            # If victim is dirty, ensure WAL is durable through page_lsn before replacing
            if frame.is_dirty and frame.page is not None and frame.page_id is not None:
                self._ensure_wal_flushed_for_frame(frame)
                self._disk_manager.write_page(frame.page_id, frame.page)
                self._disk_manager.flush()
                self._dirty_page_table.clean_page(frame.page_id)
                frame.is_dirty = False

            # Remove old page mapping
            if frame.page_id is not None and frame.page_id in self._page_table:
                del self._page_table[frame.page_id]

        # 4. Populate frame with new page
        frame.load_page(page_id=page_id, page=page, pin_count=1, is_dirty=False)
        self._replacer.pin(frame_id)
        self._page_table[page_id] = frame_id

        return frame.page

    def allocate_page(self) -> Page:
        """Allocate a new page on disk and fetch it into the buffer pool.

        Returns:
            The newly allocated and pinned in-memory Page instance.
        """
        self._check_open()
        new_page_id = self._disk_manager.allocate_page()
        return self.fetch_page(new_page_id)

    def unpin_page(self, page_id: int, is_dirty: bool = False) -> bool:
        """Decrement pin count of a resident page and optionally mark it dirty.

        Args:
            page_id: Target page ID.
            is_dirty: If True, marks the resident page as dirty.

        Returns:
            True on successful unpin.

        Raises:
            BufferManagerClosedError: If the manager is closed.
            InvalidPageIdError: If page_id is invalid.
            PageNotResidentError: If page is not currently in the buffer pool.
            BufferManagerError: If pin count is already 0.
        """
        self._check_open()

        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
            raise InvalidPageIdError(
                f"Page ID must be a non-negative integer, got {page_id!r}"
            )

        if page_id not in self._page_table:
            raise PageNotResidentError(
                f"Page {page_id} is not resident in the buffer pool."
            )

        frame_id = self._page_table[page_id]
        frame = self._frames[frame_id]

        if frame.pin_count <= 0:
            raise BufferManagerError(
                f"Cannot unpin page {page_id}: pin_count is already 0."
            )

        frame.unpin(is_dirty=is_dirty)
        if is_dirty and frame.page_lsn > 0:
            self._dirty_page_table.set_dirty(page_id, frame.page_lsn)

        if frame.pin_count == 0:
            self._replacer.unpin(frame_id)

        return True

    def flush_page(self, page_id: int) -> None:
        """Write a resident dirty page to disk and mark it clean.

        Enforces WAL durability through page_lsn before writing to disk and cleans DPT.

        Args:
            page_id: Page ID to flush.

        Raises:
            BufferManagerClosedError: If the manager is closed.
            InvalidPageIdError: If page_id is invalid.
            PageNotResidentError: If the page is not in the buffer pool.
        """
        self._check_open()

        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
            raise InvalidPageIdError(
                f"Page ID must be a non-negative integer, got {page_id!r}"
            )

        if page_id not in self._page_table:
            raise PageNotResidentError(
                f"Page {page_id} is not resident in the buffer pool."
            )

        frame_id = self._page_table[page_id]
        frame = self._frames[frame_id]

        if frame.page is not None and frame.page_id is not None:
            if frame.is_dirty:
                self._ensure_wal_flushed_for_frame(frame)
                self._disk_manager.write_page(frame.page_id, frame.page)
                self._disk_manager.flush()
                frame.is_dirty = False
                self._dirty_page_table.clean_page(frame.page_id)

    def flush_all_pages(self) -> None:
        """Flush all resident dirty pages to disk, mark them clean, and clean DPT.

        Enforces WAL durability through each page's page_lsn before disk write.
        """
        self._check_open()

        for frame in self._frames:
            if not frame.is_empty and frame.is_dirty and frame.page is not None and frame.page_id is not None:
                self._ensure_wal_flushed_for_frame(frame)
                self._disk_manager.write_page(frame.page_id, frame.page)
                frame.is_dirty = False
                self._dirty_page_table.clean_page(frame.page_id)

        self._disk_manager.flush()

    def get_page_lsn(self, page_id: int) -> int:
        """Return the page_lsn associated with a resident page frame.

        Args:
            page_id: Target page ID.

        Returns:
            Integer LSN (0 if unset or clean).

        Raises:
            PageNotResidentError: If page is not in the buffer pool.
        """
        self._check_open()
        if page_id not in self._page_table:
            raise PageNotResidentError(f"Page {page_id} is not resident in buffer pool.")
        return self._frames[self._page_table[page_id]].page_lsn

    def set_page_lsn(self, page_id: int, page_lsn: int) -> None:
        """Associate an LSN with a resident page frame, mark it dirty, and update DPT.

        Args:
            page_id: Target page ID.
            page_lsn: LSN of the log record describing the page modification.

        Raises:
            PageNotResidentError: If page is not in the buffer pool.
            ValueError: If page_lsn is invalid.
        """
        self._check_open()
        if page_id not in self._page_table:
            raise PageNotResidentError(f"Page {page_id} is not resident in buffer pool.")
        frame = self._frames[self._page_table[page_id]]
        frame.page_lsn = page_lsn
        frame.is_dirty = True
        if page_lsn > 0:
            self._dirty_page_table.set_dirty(page_id, page_lsn)

    def is_dirty(self, page_id: int) -> bool:
        """Check whether a resident page is dirty.

        Args:
            page_id: Target page ID.

        Returns:
            True if resident and dirty, False if resident and clean.

        Raises:
            PageNotResidentError: If page is not in the buffer pool.
        """
        self._check_open()
        if page_id not in self._page_table:
            raise PageNotResidentError(f"Page {page_id} is not resident in buffer pool.")
        return self._frames[self._page_table[page_id]].is_dirty

    def get_pin_count(self, page_id: int) -> int:
        """Return the current pin count of a resident page.

        Args:
            page_id: Target page ID.

        Returns:
            Pin count integer >= 0.

        Raises:
            PageNotResidentError: If page is not in the buffer pool.
        """
        self._check_open()
        if page_id not in self._page_table:
            raise PageNotResidentError(f"Page {page_id} is not resident in buffer pool.")
        return self._frames[self._page_table[page_id]].pin_count

    def get_resident_pages(self) -> List[int]:
        """Return a list of all currently resident page IDs."""
        self._check_open()
        return list(self._page_table.keys())

    def get_frame_for_page(self, page_id: int) -> Optional[int]:
        """Return the frame ID where the page is resident, or None."""
        self._check_open()
        return self._page_table.get(page_id)

    def close(self, flush_dirty: bool = True) -> None:
        """Flush dirty pages and close the buffer manager and disk manager.

        Args:
            flush_dirty: Whether to write dirty pages to disk before closing.
        """
        if not self._is_closed:
            if flush_dirty and not self._disk_manager.is_closed:
                try:
                    self.flush_all_pages()
                except Exception:
                    pass

            self._disk_manager.close()
            self._is_closed = True
            self._page_table.clear()
            self._free_frames.clear()

    def __enter__(self) -> "BufferManager":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close(flush_dirty=True)

    def __repr__(self) -> str:
        """String representation of BufferManager."""
        status = "closed" if self.is_closed else "open"
        return (
            f"BufferManager(pool_size={self._pool_size}, "
            f"resident_pages={len(self._page_table)}, status='{status}')"
        )
