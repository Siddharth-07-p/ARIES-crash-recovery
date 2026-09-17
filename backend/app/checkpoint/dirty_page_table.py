"""Dirty Page Table (DPT) abstraction for ARIES crash recovery."""

from typing import Dict, Iterator, Optional, Tuple
from app.checkpoint.exceptions import InvalidCheckpointError


class DirtyPageTable:
    """Manages the in-memory Dirty Page Table (DPT).

    Maps `page_id -> recLSN`, where `recLSN` is the first Log Sequence Number (LSN)
    that caused the page to become dirty since its last clean flush to disk.

    Semantics:
        - If a page is clean, it is not present in the DPT.
        - When a page becomes dirty for the first time, (page_id, recLSN) is inserted.
        - Subsequent modifications to an already dirty page MUST NOT overwrite its original recLSN.
        - When a page is flushed to disk and marked clean, it is removed from the DPT.
        - If the page becomes dirty again after being flushed, its new update LSN becomes the new recLSN.
    """

    __slots__ = ("_table",)

    def __init__(self, initial_entries: Optional[Dict[int, int]] = None) -> None:
        """Initialize an empty or pre-populated DirtyPageTable.

        Args:
            initial_entries: Optional mapping of page_id -> recLSN.

        Raises:
            InvalidCheckpointError: If any initial entry contains invalid page_id or recLSN.
        """
        self._table: Dict[int, int] = {}
        if initial_entries is not None:
            for page_id, rec_lsn in initial_entries.items():
                self._validate_page_id(page_id)
                self._validate_rec_lsn(rec_lsn)
                self._table[page_id] = rec_lsn

    @staticmethod
    def _validate_page_id(page_id: int) -> None:
        """Validate page identifier."""
        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
            raise InvalidCheckpointError(
                f"page_id must be a non-negative integer, got {page_id!r}"
            )

    @staticmethod
    def _validate_rec_lsn(rec_lsn: int) -> None:
        """Validate recovery LSN."""
        if not isinstance(rec_lsn, int) or isinstance(rec_lsn, bool) or rec_lsn <= 0:
            raise InvalidCheckpointError(
                f"recLSN must be a positive integer, got {rec_lsn!r}"
            )

    def set_dirty(self, page_id: int, rec_lsn: int) -> bool:
        """Mark a page dirty in the DPT.

        If the page is not currently in the DPT, inserts (page_id, rec_lsn).
        If the page is already dirty in the DPT, preserves the existing recLSN unchanged.

        Args:
            page_id: Database page identifier.
            rec_lsn: LSN of the update causing the page modification.

        Returns:
            True if the page was newly added, False if it was already dirty.

        Raises:
            InvalidCheckpointError: If page_id or rec_lsn is invalid.
        """
        self._validate_page_id(page_id)
        self._validate_rec_lsn(rec_lsn)

        if page_id not in self._table:
            self._table[page_id] = rec_lsn
            return True
        return False

    def clean_page(self, page_id: int) -> Optional[int]:
        """Remove a page from the DPT when it is flushed clean to disk.

        Args:
            page_id: Database page identifier.

        Returns:
            The previous recLSN if the page was in the DPT, or None.

        Raises:
            InvalidCheckpointError: If page_id is invalid.
        """
        self._validate_page_id(page_id)
        return self._table.pop(page_id, None)

    def remove_page(self, page_id: int) -> Optional[int]:
        """Alias for clean_page."""
        return self.clean_page(page_id)

    def get_rec_lsn(self, page_id: int) -> Optional[int]:
        """Retrieve the recLSN for a given page.

        Args:
            page_id: Database page identifier.

        Returns:
            The recLSN integer if dirty, or None if clean.

        Raises:
            InvalidCheckpointError: If page_id is invalid.
        """
        self._validate_page_id(page_id)
        return self._table.get(page_id)

    def is_dirty(self, page_id: int) -> bool:
        """Check if a page is currently dirty in the DPT.

        Args:
            page_id: Database page identifier.

        Returns:
            True if dirty, False otherwise.
        """
        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
            return False
        return page_id in self._table

    def get_table(self) -> Dict[int, int]:
        """Return a snapshot copy of the full Dirty Page Table."""
        return dict(self._table)

    def get_snapshot(self) -> Dict[int, int]:
        """Return a snapshot copy of the full Dirty Page Table (deterministic sorted by page_id)."""
        return {k: self._table[k] for k in sorted(self._table.keys())}

    def clear(self) -> None:
        """Clear all entries from the DPT."""
        self._table.clear()

    def __len__(self) -> int:
        """Return number of dirty pages."""
        return len(self._table)

    def __contains__(self, page_id: object) -> bool:
        """Check membership in DPT."""
        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 0:
            return False
        return page_id in self._table

    def __getitem__(self, page_id: int) -> int:
        """Access recLSN via indexing syntax."""
        self._validate_page_id(page_id)
        if page_id not in self._table:
            raise KeyError(f"Page {page_id} is not in Dirty Page Table")
        return self._table[page_id]

    def __iter__(self) -> Iterator[Tuple[int, int]]:
        """Iterate over (page_id, recLSN) pairs sorted by page_id."""
        for page_id in sorted(self._table.keys()):
            yield page_id, self._table[page_id]

    def __repr__(self) -> str:
        """String representation of DirtyPageTable."""
        return f"DirtyPageTable(entries={dict(self._table)})"
