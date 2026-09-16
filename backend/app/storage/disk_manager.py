"""DiskManager responsible for physical database-file I/O operations."""

import os
from pathlib import Path
from typing import Union, BinaryIO, Optional

from app.storage.constants import PAGE_SIZE, DEFAULT_DB_PATH
from app.storage.exceptions import (
    CorruptedDatabaseFileError,
    DiskManagerClosedError,
    InvalidPageIdError,
    InvalidPageSizeError,
    PageNotFoundError,
    StorageError,
)
from app.storage.page import Page


class DiskManager:
    """Manages physical I/O operations for database pages in a single binary file.

    Offset calculation:
        offset = page_id * PAGE_SIZE

    Example:
        Page 0 -> offset 0
        Page 1 -> offset 4096
        Page 2 -> offset 8192
    """

    def __init__(self, db_path: Union[str, Path] = DEFAULT_DB_PATH) -> None:
        """Initialize DiskManager and open or create the database file.

        Args:
            db_path: File system path to the database file.

        Raises:
            CorruptedDatabaseFileError: If the existing file size is not a multiple of PAGE_SIZE.
            StorageError: If the file cannot be created or opened.
        """
        self._db_path = Path(db_path)
        self._is_closed = False
        self._file: Optional[BinaryIO] = None

        # Ensure parent directory exists
        if self._db_path.parent:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Create file if it does not exist
            if not self._db_path.exists():
                with open(self._db_path, "wb"):
                    pass

            # Validate existing file size
            file_size = self._db_path.stat().st_size
            if file_size % PAGE_SIZE != 0:
                raise CorruptedDatabaseFileError(
                    f"Database file '{self._db_path}' size ({file_size} bytes) is not a multiple of PAGE_SIZE ({PAGE_SIZE} bytes)."
                )

            # Open file in binary read/write mode without truncating
            self._file = open(self._db_path, "r+b")
        except CorruptedDatabaseFileError:
            raise
        except Exception as err:
            raise StorageError(
                f"Failed to open database file at '{self._db_path}': {err}"
            ) from err

    @property
    def db_path(self) -> Path:
        """Return the database file path."""
        return self._db_path

    @property
    def is_closed(self) -> bool:
        """Check whether the DiskManager is closed."""
        return self._is_closed or self._file is None or self._file.closed

    def _check_open(self) -> None:
        """Ensure the DiskManager is open and ready for I/O operations.

        Raises:
            DiskManagerClosedError: If the DiskManager is closed.
        """
        if self.is_closed:
            raise DiskManagerClosedError("DiskManager is closed. No operations permitted.")

    def get_page_count(self) -> int:
        """Calculate the total number of allocated pages in the database file.

        Returns:
            Number of pages currently stored.

        Raises:
            DiskManagerClosedError: If the manager is closed.
            CorruptedDatabaseFileError: If the file size is not a multiple of PAGE_SIZE.
        """
        self._check_open()
        assert self._file is not None

        self._file.seek(0, os.SEEK_END)
        size = self._file.tell()

        if size % PAGE_SIZE != 0:
            raise CorruptedDatabaseFileError(
                f"Database file size ({size} bytes) is corrupted; not a multiple of PAGE_SIZE ({PAGE_SIZE} bytes)."
            )

        return size // PAGE_SIZE

    def allocate_page(self) -> int:
        """Allocate a new zero-initialized page at the end of the database file.

        Returns:
            The newly assigned page_id.

        Raises:
            DiskManagerClosedError: If the manager is closed.
            StorageError: If the page cannot be allocated or written.
        """
        self._check_open()
        assert self._file is not None

        new_page_id = self.get_page_count()
        offset = new_page_id * PAGE_SIZE

        try:
            self._file.seek(offset)
            # Write PAGE_SIZE zero bytes
            zero_bytes = b"\x00" * PAGE_SIZE
            self._file.write(zero_bytes)
            self._file.flush()
            os.fsync(self._file.fileno())
        except Exception as err:
            raise StorageError(
                f"Failed to allocate page {new_page_id} at offset {offset}: {err}"
            ) from err

        return new_page_id

    def read_page(self, page_id: int) -> Page:
        """Read a database page by its page ID from disk.

        Args:
            page_id: Unique identifier of the page to read.

        Returns:
            Page instance populated with disk data.

        Raises:
            DiskManagerClosedError: If the manager is closed.
            InvalidPageIdError: If page_id is not a non-negative integer.
            PageNotFoundError: If page_id exceeds the current page count.
            CorruptedDatabaseFileError: If incomplete page data is read.
        """
        self._check_open()
        assert self._file is not None

        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
            raise InvalidPageIdError(
                f"Page ID must be a non-negative integer, got {page_id!r} (type={type(page_id).__name__})"
            )

        total_pages = self.get_page_count()
        if page_id >= total_pages:
            raise PageNotFoundError(
                f"Page {page_id} not found. Total allocated pages: {total_pages}."
            )

        offset = page_id * PAGE_SIZE
        self._file.seek(offset)
        data = self._file.read(PAGE_SIZE)

        if len(data) != PAGE_SIZE:
            raise CorruptedDatabaseFileError(
                f"Incomplete read for page {page_id} at offset {offset}: expected {PAGE_SIZE} bytes, got {len(data)} bytes."
            )

        return Page(page_id=page_id, data=data)

    def write_page(self, page_id: int, page: Page) -> None:
        """Write a database page to its corresponding physical location on disk.

        Args:
            page_id: Target page ID.
            page: Page instance to persist.

        Raises:
            DiskManagerClosedError: If the manager is closed.
            InvalidPageIdError: If page_id is invalid or does not match page.page_id.
            TypeError: If page is not an instance of Page.
            InvalidPageSizeError: If page buffer size does not match PAGE_SIZE.
            PageNotFoundError: If target page_id is not yet allocated.
        """
        self._check_open()
        assert self._file is not None

        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
            raise InvalidPageIdError(
                f"Page ID must be a non-negative integer, got {page_id!r} (type={type(page_id).__name__})"
            )

        if not isinstance(page, Page):
            raise TypeError(
                f"Expected a Page instance for write_page, got {type(page).__name__}"
            )

        if page.page_id != page_id:
            raise InvalidPageIdError(
                f"Mismatched page ID: write_page target is {page_id}, but Page object ID is {page.page_id}"
            )

        page_bytes = page.to_bytes()
        if len(page_bytes) != PAGE_SIZE:
            raise InvalidPageSizeError(
                f"Page byte length {len(page_bytes)} does not match PAGE_SIZE {PAGE_SIZE}"
            )

        total_pages = self.get_page_count()
        if page_id >= total_pages:
            raise PageNotFoundError(
                f"Cannot write to unallocated page {page_id}. Total allocated pages: {total_pages}."
            )

        offset = page_id * PAGE_SIZE
        self._file.seek(offset)
        self._file.write(page_bytes)

    def flush(self) -> None:
        """Flush pending write buffers to disk and sync with filesystem.

        Raises:
            DiskManagerClosedError: If the manager is closed.
        """
        self._check_open()
        assert self._file is not None

        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        """Flush buffers and close the underlying database file."""
        if not self._is_closed and self._file is not None:
            try:
                if not self._file.closed:
                    self._file.flush()
                    os.fsync(self._file.fileno())
                    self._file.close()
            finally:
                self._is_closed = True
                self._file = None

    def __enter__(self) -> "DiskManager":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit, automatically closing the disk manager."""
        self.close()

    def __repr__(self) -> str:
        """String representation of DiskManager."""
        status = "closed" if self.is_closed else "open"
        return f"DiskManager(db_path='{self._db_path}', status='{status}')"
