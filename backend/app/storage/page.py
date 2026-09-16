"""Page abstraction representing a single fixed-size database page."""

from typing import Union
from app.storage.constants import PAGE_SIZE
from app.storage.exceptions import (
    InvalidPageIdError,
    InvalidPageSizeError,
    PageOutOfBoundsError,
)


class Page:
    """Fixed-size binary database page in memory.
    
    Attributes:
        page_id: Non-negative integer uniquely identifying the page.
    """

    __slots__ = ("_page_id", "_data")

    def __init__(
        self,
        page_id: int,
        data: Union[bytes, bytearray, memoryview, None] = None,
    ) -> None:
        """Initialize a Page.

        Args:
            page_id: Unique non-negative integer identifier.
            data: Optional raw bytes of length PAGE_SIZE. If None, initialized to zeros.

        Raises:
            InvalidPageIdError: If page_id is not a non-negative integer.
            InvalidPageSizeError: If data length is not exactly PAGE_SIZE.
        """
        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
            raise InvalidPageIdError(
                f"Page ID must be a non-negative integer, got {page_id!r} (type={type(page_id).__name__})"
            )

        self._page_id: int = page_id

        if data is None:
            self._data: bytearray = bytearray(PAGE_SIZE)
        else:
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise TypeError(
                    f"Page data must be bytes-like object, got {type(data).__name__}"
                )
            if len(data) != PAGE_SIZE:
                raise InvalidPageSizeError(
                    f"Page data must be exactly {PAGE_SIZE} bytes, got {len(data)} bytes"
                )
            self._data = bytearray(data)

    @property
    def page_id(self) -> int:
        """Return the page ID."""
        return self._page_id

    @property
    def page_size(self) -> int:
        """Return the configured page size."""
        return PAGE_SIZE

    def read(self, offset: int, length: int) -> bytes:
        """Read a slice of bytes from the page.

        Args:
            offset: Starting byte offset within the page (0-indexed).
            length: Number of bytes to read.

        Returns:
            Immutable bytes read from the specified range.

        Raises:
            PageOutOfBoundsError: If offset or length is invalid, negative,
                or extends beyond PAGE_SIZE.
        """
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise PageOutOfBoundsError(
                f"Offset must be an integer, got {offset!r} (type={type(offset).__name__})"
            )
        if not isinstance(length, int) or isinstance(length, bool):
            raise PageOutOfBoundsError(
                f"Length must be an integer, got {length!r} (type={type(length).__name__})"
            )
        if offset < 0:
            raise PageOutOfBoundsError(f"Offset cannot be negative: {offset}")
        if length < 0:
            raise PageOutOfBoundsError(f"Length cannot be negative: {length}")
        if offset + length > PAGE_SIZE:
            raise PageOutOfBoundsError(
                f"Read range [{offset} : {offset + length}] exceeds page size {PAGE_SIZE}"
            )

        return bytes(self._data[offset : offset + length])

    def write(self, offset: int, data: Union[bytes, bytearray, memoryview]) -> None:
        """Write bytes into the page at a specific offset.

        Args:
            offset: Starting byte offset within the page (0-indexed).
            data: Bytes-like data to write into the page buffer.

        Raises:
            PageOutOfBoundsError: If offset is negative, beyond page size,
                or if the write extends beyond PAGE_SIZE.
            TypeError: If data is not a bytes-like object.
        """
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise PageOutOfBoundsError(
                f"Offset must be an integer, got {offset!r} (type={type(offset).__name__})"
            )
        if offset < 0:
            raise PageOutOfBoundsError(f"Offset cannot be negative: {offset}")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(
                f"Data to write must be bytes-like object, got {type(data).__name__}"
            )

        data_len = len(data)
        if offset + data_len > PAGE_SIZE:
            raise PageOutOfBoundsError(
                f"Write range [{offset} : {offset + data_len}] exceeds page size {PAGE_SIZE}"
            )

        self._data[offset : offset + data_len] = data

    def to_bytes(self) -> bytes:
        """Convert page buffer into immutable raw bytes for persistence.

        Returns:
            Raw bytes of length PAGE_SIZE.
        """
        return bytes(self._data)

    @classmethod
    def from_bytes(
        cls,
        page_id: int,
        data: Union[bytes, bytearray, memoryview],
    ) -> "Page":
        """Construct a Page instance from raw binary data.

        Args:
            page_id: Page identifier.
            data: Raw binary data of length PAGE_SIZE.

        Returns:
            A new Page instance.
        """
        return cls(page_id=page_id, data=data)

    def clear(self) -> None:
        """Zero out the entire page buffer."""
        self._data[:] = b"\x00" * PAGE_SIZE

    def __len__(self) -> int:
        """Return the fixed page size."""
        return PAGE_SIZE

    def __eq__(self, other: object) -> bool:
        """Check equality with another Page instance."""
        if not isinstance(other, Page):
            return False
        return self._page_id == other._page_id and self._data == other._data

    def __repr__(self) -> str:
        """String representation of the Page object."""
        return f"Page(page_id={self._page_id}, size={PAGE_SIZE})"
