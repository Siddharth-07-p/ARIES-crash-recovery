"""Comprehensive test suite for Phase 1: Page-Based Persistent Storage."""

import os
from pathlib import Path
import pytest

from app.storage.constants import PAGE_SIZE
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
from app.storage.disk_manager import DiskManager


# =====================================================================
# Page Unit Tests
# =====================================================================


class TestPageAbstraction:
    """Tests for the in-memory Page abstraction."""

    def test_page_creation_default_zeros(self):
        """A new page without explicit data must have exactly PAGE_SIZE zero bytes."""
        page = Page(page_id=0)
        assert page.page_id == 0
        assert page.page_size == PAGE_SIZE
        assert len(page) == PAGE_SIZE
        assert page.to_bytes() == b"\x00" * PAGE_SIZE

    def test_page_creation_with_valid_data(self):
        """A page created with existing raw bytes of PAGE_SIZE must store that data."""
        raw_data = b"A" * PAGE_SIZE
        page = Page(page_id=5, data=raw_data)
        assert page.page_id == 5
        assert page.to_bytes() == raw_data

    def test_page_creation_invalid_data_size(self):
        """Passing data that is not exactly PAGE_SIZE must raise InvalidPageSizeError."""
        with pytest.raises(InvalidPageSizeError):
            Page(page_id=0, data=b"too short")

        with pytest.raises(InvalidPageSizeError):
            Page(page_id=0, data=b"X" * (PAGE_SIZE + 1))

    def test_page_creation_invalid_id(self):
        """Negative IDs, booleans, floats, strings must be rejected."""
        for invalid_id in [-1, -100, True, False, 3.14, "0", None]:
            with pytest.raises(InvalidPageIdError):
                Page(page_id=invalid_id)

    def test_page_byte_write_and_read(self):
        """Writing bytes at an offset and reading them back must return exact content."""
        page = Page(page_id=1)
        test_payload = b"Hello ARIES Crash Recovery"
        offset = 128
        page.write(offset, test_payload)

        read_back = page.read(offset, len(test_payload))
        assert read_back == test_payload

        # Surrounding bytes must still be zeroes
        assert page.read(0, offset) == b"\x00" * offset
        assert page.read(offset + len(test_payload), 10) == b"\x00" * 10

    def test_page_boundary_validation_read(self):
        """Reading outside page boundaries must raise PageOutOfBoundsError."""
        page = Page(page_id=0)

        # Negative offset
        with pytest.raises(PageOutOfBoundsError):
            page.read(-1, 10)

        # Negative length
        with pytest.raises(PageOutOfBoundsError):
            page.read(0, -5)

        # Read extends past PAGE_SIZE
        with pytest.raises(PageOutOfBoundsError):
            page.read(PAGE_SIZE - 10, 11)

        # Offset beyond PAGE_SIZE
        with pytest.raises(PageOutOfBoundsError):
            page.read(PAGE_SIZE + 1, 1)

        # Non-integer arguments
        with pytest.raises(PageOutOfBoundsError):
            page.read("0", 10)
        with pytest.raises(PageOutOfBoundsError):
            page.read(0, "10")

    def test_page_boundary_validation_write(self):
        """Writing outside page boundaries must raise PageOutOfBoundsError."""
        page = Page(page_id=0)

        # Negative offset
        with pytest.raises(PageOutOfBoundsError):
            page.write(-1, b"test")

        # Write extends past PAGE_SIZE
        overflow_data = b"X" * 100
        with pytest.raises(PageOutOfBoundsError):
            page.write(PAGE_SIZE - 50, overflow_data)

        # Offset beyond PAGE_SIZE
        with pytest.raises(PageOutOfBoundsError):
            page.write(PAGE_SIZE + 1, b"test")

        # Non-bytes data
        with pytest.raises(TypeError):
            page.write(0, "not bytes")

    def test_page_clear(self):
        """Clearing a page must reset all its bytes to zero."""
        page = Page(page_id=0)
        page.write(0, b"Important Data")
        assert page.read(0, 14) == b"Important Data"

        page.clear()
        assert page.to_bytes() == b"\x00" * PAGE_SIZE

    def test_page_from_bytes(self):
        """Page.from_bytes factory method must construct a valid Page object."""
        data = b"\x42" * PAGE_SIZE
        page = Page.from_bytes(page_id=3, data=data)
        assert page.page_id == 3
        assert page.to_bytes() == data

    def test_page_repr_and_equality(self):
        """Page equality and repr string checks."""
        page1 = Page(page_id=1)
        page2 = Page(page_id=1)
        page3 = Page(page_id=2)

        page1.write(0, b"Alpha")
        page2.write(0, b"Alpha")

        assert page1 == page2
        assert page1 != page3
        assert "Page(page_id=1" in repr(page1)


# =====================================================================
# DiskManager Tests
# =====================================================================


class TestDiskManager:
    """Tests for physical database file page I/O and DiskManager."""

    def test_database_file_creation(self, tmp_path: Path):
        """Opening a non-existent database file must create it as a zero-byte file."""
        db_file = tmp_path / "test.db"
        assert not db_file.exists()

        dm = DiskManager(db_file)
        assert db_file.exists()
        assert db_file.stat().st_size == 0
        assert dm.get_page_count() == 0
        dm.close()

    def test_page_allocation_and_count(self, tmp_path: Path):
        """Allocating pages must increase page count and extend file by exact multiples of PAGE_SIZE."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            assert dm.get_page_count() == 0

            p0 = dm.allocate_page()
            assert p0 == 0
            assert dm.get_page_count() == 1
            assert db_file.stat().st_size == PAGE_SIZE

            p1 = dm.allocate_page()
            assert p1 == 1
            assert dm.get_page_count() == 2
            assert db_file.stat().st_size == 2 * PAGE_SIZE

            p2 = dm.allocate_page()
            assert p2 == 2
            assert dm.get_page_count() == 3
            assert db_file.stat().st_size == 3 * PAGE_SIZE

    def test_read_newly_allocated_page(self, tmp_path: Path):
        """A newly allocated page must be read from disk containing all zeroes."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            page_id = dm.allocate_page()
            page = dm.read_page(page_id)
            assert page.page_id == page_id
            assert page.to_bytes() == b"\x00" * PAGE_SIZE

    def test_single_page_persistence_across_reopen(self, tmp_path: Path):
        """Verify data written to a page survives disk manager close and reopen."""
        db_file = tmp_path / "test.db"
        magic_bytes = b"Hello ARIES Crash Recovery System Persistence Test"

        # 1. Open and allocate
        dm1 = DiskManager(db_file)
        page_id = dm1.allocate_page()
        page = dm1.read_page(page_id)

        # 2. Write known data
        page.write(64, magic_bytes)
        dm1.write_page(page_id, page)
        dm1.flush()

        # 3. Close database
        dm1.close()
        assert dm1.is_closed

        # 4. Reopen database in a completely new DiskManager instance
        dm2 = DiskManager(db_file)
        assert dm2.get_page_count() == 1

        # 5. Read back the page from disk
        reopened_page = dm2.read_page(page_id)
        assert reopened_page.page_id == page_id
        assert reopened_page.read(64, len(magic_bytes)) == magic_bytes
        dm2.close()

    def test_multiple_page_persistence_and_isolation(self, tmp_path: Path):
        """Multiple pages written with distinct data remain isolated and persistent."""
        db_file = tmp_path / "test.db"
        num_pages = 5
        test_payloads = {
            i: f"PAGE_PAYLOAD_{i}_{'X' * 50}".encode("utf-8") for i in range(num_pages)
        }

        # Session 1: Allocate and write all pages
        with DiskManager(db_file) as dm:
            for i in range(num_pages):
                pid = dm.allocate_page()
                assert pid == i
                p = Page(page_id=pid)
                p.write(0, test_payloads[i])
                dm.write_page(pid, p)
            dm.flush()

        # Session 2: Reopen and verify every page has exact content
        with DiskManager(db_file) as dm:
            assert dm.get_page_count() == num_pages
            for i in range(num_pages):
                page = dm.read_page(i)
                expected = test_payloads[i]
                assert page.read(0, len(expected)) == expected

            # Modify Page 2 only
            p2 = dm.read_page(2)
            modified_payload = b"PAGE_2_MODIFIED_CONTENT_12345"
            p2.write(0, modified_payload)
            dm.write_page(2, p2)
            dm.flush()

        # Session 3: Reopen and verify Page 2 was updated and all other pages were unchanged
        with DiskManager(db_file) as dm:
            for i in range(num_pages):
                page = dm.read_page(i)
                if i == 2:
                    assert page.read(0, len(modified_payload)) == modified_payload
                else:
                    expected = test_payloads[i]
                    assert page.read(0, len(expected)) == expected

    def test_read_unallocated_page_error(self, tmp_path: Path):
        """Reading an unallocated or non-existent page must raise PageNotFoundError."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            dm.allocate_page()  # allocates page 0
            with pytest.raises(PageNotFoundError):
                dm.read_page(1)
            with pytest.raises(PageNotFoundError):
                dm.read_page(999)

    def test_write_unallocated_page_error(self, tmp_path: Path):
        """Writing to an unallocated page must raise PageNotFoundError."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            p = Page(page_id=0)
            with pytest.raises(PageNotFoundError):
                dm.write_page(0, p)

    def test_invalid_page_id_validation(self, tmp_path: Path):
        """Negative or non-integer page IDs must raise InvalidPageIdError."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            dm.allocate_page()
            for bad_id in [-1, -10, True, False, "0", 1.5]:
                with pytest.raises(InvalidPageIdError):
                    dm.read_page(bad_id)

                dummy_page = Page(page_id=0)
                with pytest.raises(InvalidPageIdError):
                    dm.write_page(bad_id, dummy_page)

    def test_write_page_mismatched_id(self, tmp_path: Path):
        """Writing a Page object whose internal page_id doesn't match target page_id must fail."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            dm.allocate_page()
            dm.allocate_page()
            page_0 = Page(page_id=0)

            # Target page_id is 1, but page_0.page_id is 0
            with pytest.raises(InvalidPageIdError):
                dm.write_page(1, page_0)

    def test_write_page_invalid_type(self, tmp_path: Path):
        """Passing non-Page object to write_page must raise TypeError."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            dm.allocate_page()
            with pytest.raises(TypeError):
                dm.write_page(0, b"raw bytes")

    def test_corrupted_database_file_size_on_open(self, tmp_path: Path):
        """If database file size is not a multiple of PAGE_SIZE, DiskManager must raise CorruptedDatabaseFileError."""
        db_file = tmp_path / "corrupt.db"
        # Write 500 bytes (not a multiple of 4096)
        with open(db_file, "wb") as f:
            f.write(b"CORRUPTED_FILE_DATA" * 10)

        with pytest.raises(CorruptedDatabaseFileError):
            DiskManager(db_file)

    def test_corrupted_database_file_size_on_operation(self, tmp_path: Path):
        """If database file size is externally truncated/altered, get_page_count must raise CorruptedDatabaseFileError."""
        db_file = tmp_path / "test.db"
        dm = DiskManager(db_file)
        dm.allocate_page()
        # Truncate file externally to an invalid length
        with open(db_file, "wb") as f:
            f.write(b"X" * 100)

        with pytest.raises(CorruptedDatabaseFileError):
            dm.get_page_count()
        dm.close()

    def test_operations_after_close_rejected(self, tmp_path: Path):
        """All operations on a closed DiskManager must raise DiskManagerClosedError."""
        db_file = tmp_path / "test.db"
        dm = DiskManager(db_file)
        dm.allocate_page()
        p0 = Page(page_id=0)
        dm.close()

        assert dm.is_closed

        with pytest.raises(DiskManagerClosedError):
            dm.get_page_count()

        with pytest.raises(DiskManagerClosedError):
            dm.allocate_page()

        with pytest.raises(DiskManagerClosedError):
            dm.read_page(0)

        with pytest.raises(DiskManagerClosedError):
            dm.write_page(0, p0)

        with pytest.raises(DiskManagerClosedError):
            dm.flush()

    def test_multiple_close_calls_safe(self, tmp_path: Path):
        """Calling close() multiple times should be safe and idempotent."""
        db_file = tmp_path / "test.db"
        dm = DiskManager(db_file)
        dm.close()
        dm.close()
        assert dm.is_closed
