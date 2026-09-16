"""Comprehensive test suite for Phase 2: Buffer Manager."""

from pathlib import Path
import pytest

from app.buffer.constants import DEFAULT_BUFFER_POOL_SIZE
from app.buffer.exceptions import (
    BufferManagerClosedError,
    BufferManagerError,
    NoEvictableFrameError,
    PageNotResidentError,
)
from app.buffer.frame import Frame
from app.buffer.replacer import LRUReplacer
from app.buffer.buffer_manager import BufferManager
from app.storage.constants import PAGE_SIZE
from app.storage.disk_manager import DiskManager
from app.storage.exceptions import InvalidPageIdError, PageNotFoundError
from app.storage.page import Page


# =====================================================================
# Unit Tests: Frame & LRUReplacer
# =====================================================================


class TestFrameAndReplacer:
    """Unit tests for Frame and LRUReplacer data structures."""

    def test_frame_initial_state(self):
        """A new frame must be empty, unpinned, and clean."""
        frame = Frame(frame_id=0)
        assert frame.frame_id == 0
        assert frame.page_id is None
        assert frame.page is None
        assert frame.pin_count == 0
        assert not frame.is_dirty
        assert frame.is_empty
        assert not frame.is_pinned

    def test_frame_load_and_pin_unpin(self):
        """Loading a page into a frame and adjusting pin counts."""
        frame = Frame(frame_id=1)
        page = Page(page_id=10)
        frame.load_page(page_id=10, page=page, pin_count=1, is_dirty=False)

        assert not frame.is_empty
        assert frame.is_pinned
        assert frame.pin_count == 1
        assert frame.page_id == 10
        assert frame.page == page

        # Pin again
        assert frame.pin() == 2
        assert frame.pin_count == 2

        # Unpin with dirty
        assert frame.unpin(is_dirty=True) == 1
        assert frame.is_dirty
        assert frame.pin_count == 1
        assert frame.is_pinned

        # Unpin to 0
        assert frame.unpin(is_dirty=False) == 0
        assert not frame.is_pinned
        assert frame.pin_count == 0

        # Unpin past 0 must fail
        with pytest.raises(ValueError):
            frame.unpin()

    def test_lru_replacer_order(self):
        """LRUReplacer must evict in least-recently-unpinned order."""
        replacer = LRUReplacer()
        assert replacer.size() == 0
        assert replacer.victim() is None

        # Unpin frames 0, 1, 2 in order
        replacer.unpin(0)
        replacer.unpin(1)
        replacer.unpin(2)
        assert replacer.size() == 3

        # Access / re-unpin frame 0 (making it most recently used)
        replacer.unpin(0)

        # Eviction order should now be: 1, 2, 0
        assert replacer.victim() == 1
        assert replacer.victim() == 2
        assert replacer.victim() == 0
        assert replacer.victim() is None
        assert replacer.size() == 0

    def test_lru_replacer_pin_removes_from_candidates(self):
        """Pinning a frame removes it from candidate eviction list."""
        replacer = LRUReplacer()
        replacer.unpin(0)
        replacer.unpin(1)

        # Pin frame 0 -> it is no longer evictable
        replacer.pin(0)
        assert replacer.size() == 1

        assert replacer.victim() == 1
        assert replacer.victim() is None


# =====================================================================
# Integration Tests: BufferManager
# =====================================================================


class TestBufferManager:
    """Core tests for BufferManager caching, pinning, dirty tracking, and eviction."""

    def test_fetch_existing_page(self, tmp_path: Path):
        """Requirement A: Allocate/write page via DiskManager, fetch via BufferManager."""
        db_file = tmp_path / "test.db"
        test_data = b"PHASE_2_FETCH_TEST_PAYLOAD"

        with DiskManager(db_file) as dm:
            pid = dm.allocate_page()
            p = Page(page_id=pid)
            p.write(0, test_data)
            dm.write_page(pid, p)

        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=5)
            page = bm.fetch_page(pid)
            assert page.page_id == pid
            assert page.read(0, len(test_data)) == test_data
            assert bm.get_pin_count(pid) == 1
            assert not bm.is_dirty(pid)
            bm.close()

    def test_buffer_hit_reuses_frame(self, tmp_path: Path):
        """Requirement B: Fetching the same page twice reuses resident frame."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            pid = dm.allocate_page()

        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=5)
            page1 = bm.fetch_page(pid)
            frame_id1 = bm.get_frame_for_page(pid)

            # Second fetch
            page2 = bm.fetch_page(pid)
            frame_id2 = bm.get_frame_for_page(pid)

            # Must be identical in-memory object and frame
            assert page1 is page2
            assert frame_id1 == frame_id2
            assert bm.get_pin_count(pid) == 2
            bm.close()

    def test_pin_and_unpin_lifecycle(self, tmp_path: Path):
        """Requirement C: Fetch increments pin_count, unpin decrements, never negative."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            pid = dm.allocate_page()

        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=5)
            bm.fetch_page(pid)
            assert bm.get_pin_count(pid) == 1

            bm.fetch_page(pid)
            assert bm.get_pin_count(pid) == 2

            bm.unpin_page(pid, is_dirty=False)
            assert bm.get_pin_count(pid) == 1

            bm.unpin_page(pid, is_dirty=False)
            assert bm.get_pin_count(pid) == 0

            # Unpinning when pin_count is already 0 must raise BufferManagerError
            with pytest.raises(BufferManagerError):
                bm.unpin_page(pid)
            bm.close()

    def test_dirty_page_tracking_and_flush(self, tmp_path: Path):
        """Requirement D: Modify page, unpin with is_dirty=True, verify dirty state, flush."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            pid = dm.allocate_page()

        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=5)
            page = bm.fetch_page(pid)
            page.write(0, b"MODIFIED_DATA")

            assert not bm.is_dirty(pid)
            bm.unpin_page(pid, is_dirty=True)
            assert bm.is_dirty(pid)

            bm.flush_page(pid)
            assert not bm.is_dirty(pid)
            bm.close()

    def test_persistence_after_flush(self, tmp_path: Path):
        """Requirement E: Modify in buffer, flush, close and reopen DiskManager, verify."""
        db_file = tmp_path / "test.db"
        new_payload = b"PERSISTED_BUFFER_MODIFICATION"

        # 1. Allocate & modify through BufferManager
        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=5)
            page = bm.allocate_page()
            pid = page.page_id
            page.write(100, new_payload)
            bm.unpin_page(pid, is_dirty=True)
            bm.flush_page(pid)
            bm.close()

        # 2. Reopen fresh DiskManager and verify disk contents
        with DiskManager(db_file) as dm:
            disk_page = dm.read_page(pid)
            assert disk_page.read(100, len(new_payload)) == new_payload

    def test_lru_eviction_with_small_pool(self, tmp_path: Path):
        """Requirement F: In pool_size=2, unpinned pages are evicted according to LRU."""
        db_file = tmp_path / "test.db"

        with DiskManager(db_file) as dm:
            p0 = dm.allocate_page()
            p1 = dm.allocate_page()
            p2 = dm.allocate_page()

        with DiskManager(db_file) as dm:
            # Buffer pool capacity = 2 frames
            bm = BufferManager(dm, pool_size=2)

            # Load page 0 and page 1
            bm.fetch_page(p0)
            bm.fetch_page(p1)
            assert set(bm.get_resident_pages()) == {p0, p1}

            # Unpin page 0 first, then page 1 (p0 is LRU)
            bm.unpin_page(p0)
            bm.unpin_page(p1)

            # Fetch page 2 -> p0 should be evicted as LRU victim
            bm.fetch_page(p2)
            resident = bm.get_resident_pages()
            assert p0 not in resident
            assert set(resident) == {p1, p2}
            bm.close()

    def test_dirty_page_eviction_persists_to_disk(self, tmp_path: Path):
        """Requirement G: Evicting a dirty page automatically flushes it to disk."""
        db_file = tmp_path / "test.db"
        p0_dirty_data = b"DIRTY_PAGE_0_DATA_SAVED_ON_EVICTION"

        with DiskManager(db_file) as dm:
            p0 = dm.allocate_page()
            p1 = dm.allocate_page()
            p2 = dm.allocate_page()

        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=2)

            # Fetch p0, write dirty data, unpin as dirty
            page0 = bm.fetch_page(p0)
            page0.write(0, p0_dirty_data)
            bm.unpin_page(p0, is_dirty=True)

            # Fetch p1, unpin
            bm.fetch_page(p1)
            bm.unpin_page(p1)

            # Now fetch p2 -> forces eviction of p0 (which is dirty)
            bm.fetch_page(p2)
            assert p0 not in bm.get_resident_pages()
            bm.close()

        # Reopen DiskManager and verify p0 contains the dirty data on disk
        with DiskManager(db_file) as dm:
            disk_page0 = dm.read_page(p0)
            assert disk_page0.read(0, len(p0_dirty_data)) == p0_dirty_data

    def test_pinned_page_protection_and_buffer_full(self, tmp_path: Path):
        """Requirement H: Pinned pages cannot be evicted; full pool raises NoEvictableFrameError."""
        db_file = tmp_path / "test.db"

        with DiskManager(db_file) as dm:
            p0 = dm.allocate_page()
            p1 = dm.allocate_page()
            p2 = dm.allocate_page()

        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=2)

            # Fetch and keep pinned
            bm.fetch_page(p0)
            bm.fetch_page(p1)
            assert bm.get_pin_count(p0) == 1
            assert bm.get_pin_count(p1) == 1

            # Attempting to fetch p2 when all frames are pinned must fail
            with pytest.raises(NoEvictableFrameError):
                bm.fetch_page(p2)

            # Unpin p0 -> now p2 fetch succeeds by evicting p0
            bm.unpin_page(p0)
            bm.fetch_page(p2)
            assert p0 not in bm.get_resident_pages()
            assert p1 in bm.get_resident_pages()
            assert p2 in bm.get_resident_pages()
            bm.close()

    def test_multiple_page_isolation(self, tmp_path: Path):
        """Requirement I: Multiple pages in buffer remain isolated when one is modified."""
        db_file = tmp_path / "test.db"

        with DiskManager(db_file) as dm:
            p0 = dm.allocate_page()
            p1 = dm.allocate_page()

        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=5)
            page0 = bm.fetch_page(p0)
            page1 = bm.fetch_page(p1)

            page0.write(0, b"PAGE_ZERO_EXCLUSIVE")
            # Verify page1 buffer remains unchanged
            assert page1.read(0, 19) == b"\x00" * 19
            bm.close()

    def test_flush_all_pages(self, tmp_path: Path):
        """flush_all_pages flushes all dirty resident pages to disk."""
        db_file = tmp_path / "test.db"

        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=5)
            p0 = bm.allocate_page()
            p1 = bm.allocate_page()
            p0.write(0, b"DATA_P0")
            p1.write(0, b"DATA_P1")
            bm.unpin_page(p0.page_id, is_dirty=True)
            bm.unpin_page(p1.page_id, is_dirty=True)

            assert bm.is_dirty(p0.page_id)
            assert bm.is_dirty(p1.page_id)

            bm.flush_all_pages()

            assert not bm.is_dirty(p0.page_id)
            assert not bm.is_dirty(p1.page_id)
            bm.close()

        with DiskManager(db_file) as dm:
            assert dm.read_page(0).read(0, 7) == b"DATA_P0"
            assert dm.read_page(1).read(0, 7) == b"DATA_P1"

    def test_operations_on_non_resident_page(self, tmp_path: Path):
        """Unpin, flush, is_dirty on non-resident page raises PageNotResidentError."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            dm.allocate_page()

        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=5)
            with pytest.raises(PageNotResidentError):
                bm.unpin_page(99)
            with pytest.raises(PageNotResidentError):
                bm.flush_page(99)
            with pytest.raises(PageNotResidentError):
                bm.is_dirty(99)
            with pytest.raises(PageNotResidentError):
                bm.get_pin_count(99)
            bm.close()

    def test_operations_after_buffer_manager_closed(self, tmp_path: Path):
        """Operations after BufferManager.close() raise BufferManagerClosedError."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            dm.allocate_page()

        with DiskManager(db_file) as dm:
            bm = BufferManager(dm, pool_size=5)
            bm.close()
            assert bm.is_closed

            with pytest.raises(BufferManagerClosedError):
                bm.fetch_page(0)
            with pytest.raises(BufferManagerClosedError):
                bm.allocate_page()
            with pytest.raises(BufferManagerClosedError):
                bm.unpin_page(0)
            with pytest.raises(BufferManagerClosedError):
                bm.flush_page(0)
            with pytest.raises(BufferManagerClosedError):
                bm.flush_all_pages()

    def test_invalid_arguments_validation(self, tmp_path: Path):
        """Invalid page IDs and pool sizes must be rejected with appropriate errors."""
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm:
            with pytest.raises(ValueError):
                BufferManager(dm, pool_size=0)
            with pytest.raises(ValueError):
                BufferManager(dm, pool_size=-3)

            bm = BufferManager(dm, pool_size=5)
            with pytest.raises(InvalidPageIdError):
                bm.fetch_page(-1)
            with pytest.raises(InvalidPageIdError):
                bm.fetch_page("0")  # type: ignore
            with pytest.raises(PageNotFoundError):
                bm.fetch_page(100)  # non-existent page on disk
            bm.close()
