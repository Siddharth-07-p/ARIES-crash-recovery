"""Comprehensive test suite for Phase 4: Write-Ahead Logging (WAL) and LSN subsystem."""

from pathlib import Path
import struct
import zlib
import pytest

from app.buffer.buffer_manager import BufferManager
from app.storage.disk_manager import DiskManager
from app.storage.page import Page
from app.transaction.transaction_manager import TransactionManager
from app.wal.constants import DEFAULT_WAL_PATH, WAL_RECORD_MAGIC
from app.wal.exceptions import (
    InvalidLogRecordError,
    InvalidLSNError,
    LogCorruptionError,
    LogManagerClosedError,
)
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecord, LogRecordType


@pytest.fixture
def temp_wal_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary WAL filepath."""
    return tmp_path / "test_database.wal"


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary DB filepath."""
    return tmp_path / "test_database.db"


class TestWALFileAndLSN:
    """Tests A, B, C, D, E: WAL file creation, persistence, and LSN allocation."""

    def test_wal_file_creation(self, temp_wal_path: Path) -> None:
        """Test A: LogManager creates WAL file upon initialization."""
        assert not temp_wal_path.exists()
        lm = LogManager(wal_path=temp_wal_path)
        assert temp_wal_path.exists()
        assert temp_wal_path.is_file()
        assert lm.next_lsn == 1
        assert lm.flushed_lsn == 0
        lm.close()

    def test_wal_persistence_across_reopen(self, temp_wal_path: Path) -> None:
        """Test B: Append records, flush, close, reopen, and verify records remain."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lsn1 = lm.append_begin(transaction_id=1)
            lsn2 = lm.append_update(
                transaction_id=1,
                page_id=0,
                offset=10,
                before_image=b"old_data",
                after_image=b"new_data",
            )
            lsn3 = lm.append_commit(transaction_id=1)
            lm.flush()

        assert temp_wal_path.stat().st_size > 0

        # Reopen
        with LogManager(wal_path=temp_wal_path) as lm2:
            records = lm2.read_all()
            assert len(records) == 3
            assert records[0].lsn == lsn1
            assert records[0].record_type == LogRecordType.BEGIN
            assert records[1].lsn == lsn2
            assert records[1].record_type == LogRecordType.UPDATE
            assert records[2].lsn == lsn3
            assert records[2].record_type == LogRecordType.COMMIT
            assert lm2.flushed_lsn == lsn3

    def test_lsn_uniqueness(self, temp_wal_path: Path) -> None:
        """Test C: Every appended log record receives a unique LSN."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lsns = [lm.append_begin(transaction_id=i) for i in range(1, 25)]
            assert len(lsns) == len(set(lsns))

    def test_lsn_monotonicity(self, temp_wal_path: Path) -> None:
        """Test D: LSNs are strictly monotonically increasing."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lsn1 = lm.append_begin(transaction_id=1)
            lsn2 = lm.append_update(1, 0, 0, b"a", b"b")
            lsn3 = lm.append_commit(1)
            lsn4 = lm.append_begin(transaction_id=2)

            assert lsn1 < lsn2 < lsn3 < lsn4
            assert [lsn1, lsn2, lsn3, lsn4] == [1, 2, 3, 4]

    def test_lsn_continuation_after_reopen(self, temp_wal_path: Path) -> None:
        """Test E: LSN allocation continues from existing WAL after reopen without resetting."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lm.append_begin(transaction_id=1)
            lm.append_update(1, 0, 0, b"a", b"b")
            last_lsn = lm.append_commit(1)
            assert last_lsn == 3
            lm.flush()

        with LogManager(wal_path=temp_wal_path) as lm2:
            assert lm2.next_lsn == 4
            new_lsn = lm2.append_begin(transaction_id=2)
            assert new_lsn == 4
            next_update = lm2.append_update(2, 1, 0, b"x", b"y")
            assert next_update == 5


class TestLogRecordTypesAndChaining:
    """Tests F, G, H, I, J, K: Record validation, types, and prev_lsn chains."""

    def test_begin_logging(self, temp_wal_path: Path) -> None:
        """Test F: BEGIN log record structure and values."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lsn = lm.append_begin(transaction_id=42)
            rec = lm.get_log_record(lsn)
            assert rec is not None
            assert rec.lsn == lsn
            assert rec.record_type == LogRecordType.BEGIN
            assert rec.transaction_id == 42
            assert rec.prev_lsn is None

    def test_commit_logging(self, temp_wal_path: Path) -> None:
        """Test G: COMMIT log record structure and prev_lsn."""
        with LogManager(wal_path=temp_wal_path) as lm:
            b_lsn = lm.append_begin(transaction_id=42)
            c_lsn = lm.append_commit(transaction_id=42)
            rec = lm.get_log_record(c_lsn)
            assert rec is not None
            assert rec.lsn == c_lsn
            assert rec.record_type == LogRecordType.COMMIT
            assert rec.transaction_id == 42
            assert rec.prev_lsn == b_lsn

    def test_abort_logging(self, temp_wal_path: Path) -> None:
        """Test H: ABORT log record structure and prev_lsn."""
        with LogManager(wal_path=temp_wal_path) as lm:
            b_lsn = lm.append_begin(transaction_id=42)
            a_lsn = lm.append_abort(transaction_id=42)
            rec = lm.get_log_record(a_lsn)
            assert rec is not None
            assert rec.lsn == a_lsn
            assert rec.record_type == LogRecordType.ABORT
            assert rec.transaction_id == 42
            assert rec.prev_lsn == b_lsn

    def test_update_logging_details(self, temp_wal_path: Path) -> None:
        """Test I: UPDATE log record page_id, offset, before-image, after-image."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lm.append_begin(transaction_id=10)
            u_lsn = lm.append_update(
                transaction_id=10,
                page_id=5,
                offset=128,
                before_image=b"ORIGINAL_PAYLOAD",
                after_image=b"MODIFIED_PAYLOAD",
            )
            rec = lm.get_log_record(u_lsn)
            assert rec is not None
            assert rec.record_type == LogRecordType.UPDATE
            assert rec.page_id == 5
            assert rec.offset == 128
            assert rec.before_image == b"ORIGINAL_PAYLOAD"
            assert rec.after_image == b"MODIFIED_PAYLOAD"

    def test_prev_lsn_single_transaction_chain(self, temp_wal_path: Path) -> None:
        """Test J: Transaction log records form a backward-linked prev_lsn chain."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lsn1 = lm.append_begin(transaction_id=1)
            lsn2 = lm.append_update(1, 0, 0, b"v0", b"v1")
            lsn3 = lm.append_update(1, 0, 0, b"v1", b"v2")
            lsn4 = lm.append_commit(transaction_id=1)

            r1 = lm.get_log_record(lsn1)
            r2 = lm.get_log_record(lsn2)
            r3 = lm.get_log_record(lsn3)
            r4 = lm.get_log_record(lsn4)

            assert r1 is not None and r1.prev_lsn is None
            assert r2 is not None and r2.prev_lsn == lsn1
            assert r3 is not None and r3.prev_lsn == lsn2
            assert r4 is not None and r4.prev_lsn == lsn3

    def test_multiple_transactions_prev_lsn_chains(self, temp_wal_path: Path) -> None:
        """Test K: Multiple interleaved transactions maintain separate prev_lsn chains."""
        with LogManager(wal_path=temp_wal_path) as lm:
            t1_b = lm.append_begin(1)
            t2_b = lm.append_begin(2)

            t1_u1 = lm.append_update(1, 0, 0, b"a", b"b")
            t2_u1 = lm.append_update(2, 1, 0, b"x", b"y")
            t1_u2 = lm.append_update(1, 0, 0, b"b", b"c")

            t2_c = lm.append_commit(2)
            t1_a = lm.append_abort(1)

            # Check T1 chain: t1_a -> t1_u2 -> t1_u1 -> t1_b -> None
            r_t1_a = lm.get_log_record(t1_a)
            r_t1_u2 = lm.get_log_record(t1_u2)
            r_t1_u1 = lm.get_log_record(t1_u1)
            r_t1_b = lm.get_log_record(t1_b)

            assert r_t1_a is not None and r_t1_a.prev_lsn == t1_u2
            assert r_t1_u2 is not None and r_t1_u2.prev_lsn == t1_u1
            assert r_t1_u1 is not None and r_t1_u1.prev_lsn == t1_b
            assert r_t1_b is not None and r_t1_b.prev_lsn is None

            # Check T2 chain: t2_c -> t2_u1 -> t2_b -> None
            r_t2_c = lm.get_log_record(t2_c)
            r_t2_u1 = lm.get_log_record(t2_u1)
            r_t2_b = lm.get_log_record(t2_b)

            assert r_t2_c is not None and r_t2_c.prev_lsn == t2_u1
            assert r_t2_u1 is not None and r_t2_u1.prev_lsn == t2_b
            assert r_t2_b is not None and r_t2_b.prev_lsn is None


class TestWALDurabilityAndCorruption:
    """Tests L, M, N: WAL flush, corruption detection, and sequential reading."""

    def test_wal_flush_and_durability_tracking(self, temp_wal_path: Path) -> None:
        """Test L: Explicit flush updates flushed_lsn and persists bytes."""
        with LogManager(wal_path=temp_wal_path) as lm:
            assert lm.flushed_lsn == 0
            lsn1 = lm.append_begin(1)
            lsn2 = lm.append_update(1, 0, 0, b"1", b"2")
            assert lm.flushed_lsn == 0  # not flushed yet

            flushed = lm.flush()
            assert flushed == lsn2
            assert lm.flushed_lsn == lsn2

            lsn3 = lm.append_commit(1)
            assert lm.flushed_lsn == lsn2
            lm.flush_to_lsn(lsn3)
            assert lm.flushed_lsn == lsn3

    def test_wal_corruption_truncated_record(self, temp_wal_path: Path) -> None:
        """Test M1: Truncated WAL records raise LogCorruptionError on reopen."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lm.append_begin(1)
            lm.append_update(1, 0, 0, b"hello", b"world")
            lm.flush()

        # Truncate WAL file by a few bytes
        size = temp_wal_path.stat().st_size
        with open(temp_wal_path, "r+b") as f:
            f.truncate(size - 5)

        with pytest.raises(LogCorruptionError, match="Truncated|CRC32 mismatch"):
            LogManager(wal_path=temp_wal_path)

    def test_wal_corruption_invalid_magic(self, temp_wal_path: Path) -> None:
        """Test M2: Corrupted magic bytes raise LogCorruptionError."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lm.append_begin(1)
            lm.flush()

        # Overwrite magic
        with open(temp_wal_path, "r+b") as f:
            f.seek(0)
            f.write(b"XXXX")

        with pytest.raises(LogCorruptionError, match="Invalid WAL record magic"):
            LogManager(wal_path=temp_wal_path)

    def test_wal_corruption_crc_mismatch(self, temp_wal_path: Path) -> None:
        """Test M3: Bit flips in payload raise LogCorruptionError on checksum validation."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lm.append_begin(1)
            lm.append_update(1, 0, 0, b"secure_data", b"target_data")
            lm.flush()

        # Flip a byte in the payload area of record 2 (record 2 starts at byte 37, payload starts at byte 70)
        with open(temp_wal_path, "r+b") as f:
            f.seek(75)
            f.write(b"\xFF")

        with pytest.raises(LogCorruptionError, match="CRC32 mismatch"):
            LogManager(wal_path=temp_wal_path)

    def test_sequential_reading_apis(self, temp_wal_path: Path) -> None:
        """Test N: read_all, read_from(lsn), get_log_record(lsn)."""
        with LogManager(wal_path=temp_wal_path) as lm:
            for i in range(1, 6):
                lm.append_begin(i)

            all_recs = lm.read_all()
            assert len(all_recs) == 5
            assert [r.lsn for r in all_recs] == [1, 2, 3, 4, 5]

            from_3 = lm.read_from(3)
            assert len(from_3) == 3
            assert [r.lsn for r in from_3] == [3, 4, 5]

            rec2 = lm.get_log_record(2)
            assert rec2 is not None and rec2.lsn == 2
            assert lm.get_log_record(999) is None

            with pytest.raises(InvalidLSNError):
                lm.read_from(0)
            with pytest.raises(InvalidLSNError):
                lm.get_log_record(-1)


class TestTransactionManagerIntegration:
    """Test O: Transaction lifecycle logging and commit/abort durability."""

    def test_transaction_manager_lifecycle_logging(self, temp_wal_path: Path) -> None:
        """Test O1: Begin, Commit, Abort generate durable WAL records through TransactionManager."""
        with LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)

            # 1. Begin T1
            t1 = tm.begin_transaction()
            assert t1.transaction_id == 1
            assert lm.get_last_lsn(1) == 1
            rec1 = lm.get_log_record(1)
            assert rec1 is not None and rec1.record_type == LogRecordType.BEGIN

            # 2. Commit T1 -> Must be durable
            tm.commit_transaction(t1.transaction_id)
            assert t1.is_committed
            assert lm.flushed_lsn >= 2
            rec2 = lm.get_log_record(2)
            assert rec2 is not None and rec2.record_type == LogRecordType.COMMIT
            assert rec2.prev_lsn == 1

            # 3. Begin T2 & Abort T2 -> Must be durable
            t2 = tm.begin_transaction()
            assert t2.transaction_id == 2
            rec3 = lm.get_log_record(3)
            assert rec3 is not None and rec3.record_type == LogRecordType.BEGIN

            tm.abort_transaction(t2.transaction_id)
            assert t2.is_aborted
            assert lm.flushed_lsn >= 4
            rec4 = lm.get_log_record(4)
            assert rec4 is not None and rec4.record_type == LogRecordType.ABORT
            assert rec4.prev_lsn == 3

    def test_transaction_manager_without_log_manager_compatibility(self) -> None:
        """Test O2: TransactionManager continues to work standalone when no LogManager is provided."""
        tm = TransactionManager()
        t1 = tm.begin_transaction()
        assert t1.is_running
        tm.commit_transaction(t1.transaction_id)
        assert t1.is_committed


class TestPageLSNAndBufferManagerEnforcement:
    """Tests P, Q: Page LSN metadata tracking and WAL-before-page-flush ordering rule."""

    def test_page_lsn_tracking_in_buffer_manager(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Test P: Update log allocation and page_lsn association in buffer frame."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)

            page = bm.allocate_page()
            page_id = page.page_id
            assert bm.get_page_lsn(page_id) == 0

            # Transaction update workflow
            lm.append_begin(transaction_id=1)
            before_bytes = page.read(0, 10)
            after_bytes = b"0123456789"
            page.write(0, after_bytes)

            update_lsn = lm.append_update(
                transaction_id=1,
                page_id=page_id,
                offset=0,
                before_image=before_bytes,
                after_image=after_bytes,
            )

            bm.set_page_lsn(page_id, update_lsn)
            assert bm.get_page_lsn(page_id) == update_lsn
            bm.unpin_page(page_id, is_dirty=True)

            assert bm.is_dirty(page_id)
            bm.close()

    def test_wal_before_page_flush_ordering(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Test Q: Prove that a dirty page cannot be written to disk before WAL is flushed to page_lsn."""
        execution_order = []

        class MockDiskManager(DiskManager):
            def write_page(self, page_id: int, page: Page) -> None:
                execution_order.append(("DISK_WRITE", page_id))
                super().write_page(page_id, page)

        class MockLogManager(LogManager):
            def flush(self) -> int:
                execution_order.append(("WAL_FLUSH", self._next_lsn - 1))
                return super().flush()

        with MockDiskManager(db_path=temp_db_path) as dm, MockLogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)

            page = bm.allocate_page()
            page_id = page.page_id

            lm.append_begin(transaction_id=1)
            u_lsn = lm.append_update(1, page_id, 0, b"old", b"new")

            bm.set_page_lsn(page_id, u_lsn)
            bm.unpin_page(page_id, is_dirty=True)

            assert lm.flushed_lsn < u_lsn  # WAL is currently not flushed to u_lsn

            # Clear any setup calls from order log
            execution_order.clear()

            # Flush the dirty page through BufferManager
            bm.flush_page(page_id)

            # Verify execution order: WAL_FLUSH must precede DISK_WRITE
            assert len(execution_order) == 2
            assert execution_order[0] == ("WAL_FLUSH", u_lsn)
            assert execution_order[1] == ("DISK_WRITE", page_id)
            assert lm.flushed_lsn >= u_lsn
            bm.close()

    def test_wal_before_page_flush_during_lru_eviction(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Test Q2: Prove that dirty victim eviction also enforces WAL flush before disk write."""
        execution_order = []

        class MockDiskManager(DiskManager):
            def write_page(self, page_id: int, page: Page) -> None:
                execution_order.append(("DISK_WRITE", page_id))
                super().write_page(page_id, page)

        class MockLogManager(LogManager):
            def flush(self) -> int:
                execution_order.append(("WAL_FLUSH", self._next_lsn - 1))
                return super().flush()

        with MockDiskManager(db_path=temp_db_path) as dm, MockLogManager(wal_path=temp_wal_path) as lm:
            # Pool of size 2
            bm = BufferManager(disk_manager=dm, pool_size=2, log_manager=lm)

            p0 = bm.allocate_page()  # page 0
            p1 = bm.allocate_page()  # page 1

            # Update p0 and mark dirty with LSN
            lm.append_begin(1)
            u_lsn = lm.append_update(1, p0.page_id, 0, b"a", b"b")
            bm.set_page_lsn(p0.page_id, u_lsn)
            bm.unpin_page(p0.page_id, is_dirty=True)
            bm.unpin_page(p1.page_id, is_dirty=False)

            execution_order.clear()

            # Fetch a 3rd page, forcing LRU eviction of p0
            p2 = bm.allocate_page()
            bm.unpin_page(p2.page_id, is_dirty=False)

            # Verify that p0 eviction flushed WAL before writing p0 to disk
            wal_flush_indices = [i for i, (op, _) in enumerate(execution_order) if op == "WAL_FLUSH"]
            disk_write_indices = [i for i, (op, _) in enumerate(execution_order) if op == "DISK_WRITE"]

            assert len(wal_flush_indices) >= 1
            assert len(disk_write_indices) >= 1
            assert wal_flush_indices[0] < disk_write_indices[0]
            bm.close()


class TestEdgeCasesAndValidations:
    """Test validation errors, closed manager states, and invalid parameters."""

    def test_invalid_log_record_parameters(self) -> None:
        """Test validation exceptions on invalid LogRecord construction."""
        with pytest.raises(InvalidLogRecordError):
            LogRecord(lsn=0, record_type=LogRecordType.BEGIN)

        with pytest.raises(InvalidLogRecordError):
            LogRecord(lsn=1, record_type="INVALID_TYPE")

        with pytest.raises(InvalidLogRecordError):
            LogRecord(lsn=1, record_type=LogRecordType.UPDATE)  # Missing update fields

    def test_closed_log_manager_rejects_operations(self, temp_wal_path: Path) -> None:
        """Test that LogManagerClosedError is raised on closed LogManager."""
        lm = LogManager(wal_path=temp_wal_path)
        lm.close()

        with pytest.raises(LogManagerClosedError):
            lm.append_begin(1)
        with pytest.raises(LogManagerClosedError):
            lm.flush()
        with pytest.raises(LogManagerClosedError):
            lm.read_all()

    def test_update_record_bounds_and_type_validations(self) -> None:
        """Test UPDATE log record offset and bounds validation."""
        # Offset beyond page size
        with pytest.raises(InvalidLogRecordError, match="beyond PAGE_SIZE"):
            LogRecord(
                lsn=1,
                record_type=LogRecordType.UPDATE,
                transaction_id=1,
                page_id=0,
                offset=4096,
                before_image=b"a",
                after_image=b"b",
            )

        # Offset + len(after_image) > PAGE_SIZE
        with pytest.raises(InvalidLogRecordError, match="exceeds page size"):
            LogRecord(
                lsn=1,
                record_type=LogRecordType.UPDATE,
                transaction_id=1,
                page_id=0,
                offset=4090,
                before_image=b"a" * 5,
                after_image=b"b" * 10,
            )

    def test_log_record_to_dict_and_repr(self) -> None:
        """Test LogRecord to_dict and string representation."""
        rec = LogRecord(
            lsn=10,
            record_type=LogRecordType.UPDATE,
            transaction_id=2,
            prev_lsn=5,
            page_id=1,
            offset=20,
            before_image=b"abc",
            after_image=b"xyz",
        )
        d = rec.to_dict()
        assert d["lsn"] == 10
        assert d["record_type"] == "UPDATE"
        assert d["transaction_id"] == 2
        assert d["prev_lsn"] == 5
        assert d["page_id"] == 1
        assert d["offset"] == 20
        assert d["before_image_len"] == 3
        assert d["after_image_len"] == 3
        assert "LSN=10" in repr(rec)

    def test_direct_append_log_non_monotonic_lsn_error(self, temp_wal_path: Path) -> None:
        """Test that appending a LogRecord with mismatched LSN raises InvalidLSNError."""
        with LogManager(wal_path=temp_wal_path) as lm:
            bad_rec = LogRecord(lsn=99, record_type=LogRecordType.BEGIN, transaction_id=1)
            with pytest.raises(InvalidLSNError):
                lm.append_log(bad_rec)

    def test_flush_to_lsn_idempotency(self, temp_wal_path: Path) -> None:
        """Test that flush_to_lsn does not fail on already-flushed LSN."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lsn = lm.append_begin(1)
            lm.flush()
            assert lm.flushed_lsn == lsn
            # Calling flush_to_lsn with smaller or equal LSN
            result = lm.flush_to_lsn(lsn)
            assert result == lsn
            result0 = lm.flush_to_lsn(0)
            assert result0 == lsn
