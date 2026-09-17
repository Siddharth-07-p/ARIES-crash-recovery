"""Comprehensive test suite for Phase 5: Checkpointing and Dirty Page Table (DPT) subsystem."""

from pathlib import Path
import struct
import pytest

from app.buffer.buffer_manager import BufferManager
from app.checkpoint.checkpoint import (
    CheckpointInfo,
    CheckpointSnapshot,
    TransactionTableEntry,
)
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.checkpoint.dirty_page_table import DirtyPageTable
from app.checkpoint.exceptions import (
    CheckpointCorruptionError,
    CheckpointError,
    CheckpointManagerClosedError,
    InvalidCheckpointError,
)
from app.storage.disk_manager import DiskManager
from app.storage.page import Page
from app.transaction.transaction import TransactionState
from app.transaction.transaction_manager import TransactionManager
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecord, LogRecordType


@pytest.fixture
def temp_wal_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary WAL filepath."""
    return tmp_path / "test_checkpoint.wal"


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary DB filepath."""
    return tmp_path / "test_checkpoint.db"


class TestDirtyPageTableUnit:
    """Tests A, B, C, D: Direct DirtyPageTable operations and invariants."""

    def test_dpt_insertion(self) -> None:
        """Test A: Add page with recLSN and verify mapping."""
        dpt = DirtyPageTable()
        assert len(dpt) == 0
        assert not dpt.is_dirty(5)

        added = dpt.set_dirty(page_id=5, rec_lsn=27)
        assert added is True
        assert len(dpt) == 1
        assert dpt.is_dirty(5)
        assert dpt.get_rec_lsn(5) == 27
        assert dpt[5] == 27
        assert 5 in dpt

    def test_dpt_rec_lsn_preservation(self) -> None:
        """Test B: Adding an already-dirty page must NOT overwrite original recLSN."""
        dpt = DirtyPageTable()
        dpt.set_dirty(page_id=5, rec_lsn=10)
        assert dpt.get_rec_lsn(5) == 10

        # Second update on already-dirty page
        added = dpt.set_dirty(page_id=5, rec_lsn=15)
        assert added is False
        assert dpt.get_rec_lsn(5) == 10  # recLSN preserved!

        # Third update with higher LSN
        added = dpt.set_dirty(page_id=5, rec_lsn=99)
        assert added is False
        assert dpt.get_rec_lsn(5) == 10  # Still preserved

    def test_dpt_removal(self) -> None:
        """Test C: Add page, remove page, verify it is absent."""
        dpt = DirtyPageTable()
        dpt.set_dirty(page_id=5, rec_lsn=27)
        dpt.set_dirty(page_id=8, rec_lsn=31)
        assert len(dpt) == 2

        old_lsn = dpt.clean_page(5)
        assert old_lsn == 27
        assert not dpt.is_dirty(5)
        assert dpt.get_rec_lsn(5) is None
        assert 5 not in dpt
        assert len(dpt) == 1
        assert dpt.is_dirty(8)

        # Cleaning a non-existent page returns None safely
        assert dpt.clean_page(99) is None

    def test_dpt_re_dirty_behavior(self) -> None:
        """Test D: Add Page 5 at LSN 10, remove, add at LSN 20 -> recLSN becomes 20."""
        dpt = DirtyPageTable()
        dpt.set_dirty(page_id=5, rec_lsn=10)
        assert dpt.get_rec_lsn(5) == 10

        # Page is flushed and cleaned
        dpt.clean_page(5)
        assert not dpt.is_dirty(5)

        # Page is modified again at LSN 20
        dpt.set_dirty(page_id=5, rec_lsn=20)
        assert dpt.get_rec_lsn(5) == 20

    def test_dpt_validation(self) -> None:
        """Test DPT input validation for invalid page_ids and recLSNs."""
        dpt = DirtyPageTable()
        with pytest.raises(InvalidCheckpointError):
            dpt.set_dirty(page_id=-1, rec_lsn=10)
        with pytest.raises(InvalidCheckpointError):
            dpt.set_dirty(page_id=0, rec_lsn=0)
        with pytest.raises(InvalidCheckpointError):
            dpt.set_dirty(page_id=0, rec_lsn=-5)
        with pytest.raises(InvalidCheckpointError):
            dpt.get_rec_lsn(-1)


class TestBufferManagerDPTIntegration:
    """Tests E, F, G, H: Buffer Manager integration with DPT."""

    def test_buffer_manager_dpt_integration(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test E: Dirtying a page via BufferManager records it in DPT."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            p0 = bm.allocate_page()
            p1 = bm.allocate_page()

            # Pages are initially clean, not in DPT
            assert not bm.dirty_page_table.is_dirty(0)
            assert not bm.dirty_page_table.is_dirty(1)

            # Update page 0 at LSN 10
            bm.set_page_lsn(0, 10)
            assert bm.is_dirty(0)
            assert bm.dirty_page_table.is_dirty(0)
            assert bm.dirty_page_table.get_rec_lsn(0) == 10
            assert bm.get_page_lsn(0) == 10

            bm.close(flush_dirty=False)

    def test_dpt_after_second_update(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test F: Modifying same dirty page twice keeps recLSN as first UPDATE LSN."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()

            # First update at LSN 27
            bm.set_page_lsn(0, 27)
            assert bm.get_page_lsn(0) == 27
            assert bm.dirty_page_table.get_rec_lsn(0) == 27

            # Second update at LSN 35
            bm.set_page_lsn(0, 35)
            assert bm.get_page_lsn(0) == 35  # Latest page_lsn is 35
            assert bm.dirty_page_table.get_rec_lsn(0) == 27  # recLSN remains 27!

            bm.close(flush_dirty=False)

    def test_dpt_removal_after_flush(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test G: Dirty page, flush page, verify removed from DPT."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            bm.allocate_page()

            bm.set_page_lsn(0, 10)
            bm.set_page_lsn(1, 20)
            assert len(bm.dirty_page_table) == 2

            # Flush page 0
            bm.flush_page(0)
            assert not bm.is_dirty(0)
            assert not bm.dirty_page_table.is_dirty(0)
            assert bm.dirty_page_table.is_dirty(1)
            assert len(bm.dirty_page_table) == 1

            # Flush all pages
            bm.flush_all_pages()
            assert not bm.is_dirty(1)
            assert not bm.dirty_page_table.is_dirty(1)
            assert len(bm.dirty_page_table) == 0

            bm.close()

    def test_dpt_dirty_eviction(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test H: Dirty page eviction enforces WAL flush and cleans DPT."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            # Pool size 2
            bm = BufferManager(disk_manager=dm, pool_size=2, log_manager=lm)
            p0 = bm.allocate_page()  # Page 0
            p1 = bm.allocate_page()  # Page 1

            # Append WAL log record for Page 0 modification
            lsn0 = lm.append_update(
                transaction_id=1,
                page_id=0,
                offset=0,
                before_image=b"\x00" * 10,
                after_image=b"\xAA" * 10,
            )
            p0.write(0, b"\xAA" * 10)
            bm.set_page_lsn(0, lsn0)
            bm.unpin_page(0, is_dirty=True)
            bm.unpin_page(1, is_dirty=False)

            assert bm.dirty_page_table.is_dirty(0)
            assert lm.flushed_lsn < lsn0  # Not yet flushed to disk

            # Allocating page 2 triggers LRU eviction of unpinned page 0
            p2 = bm.allocate_page()

            # Verify WAL was forced up to lsn0 before page 0 was written to disk
            assert lm.flushed_lsn >= lsn0
            # Page 0 was persisted to disk, so it must be removed from DPT
            assert not bm.dirty_page_table.is_dirty(0)

            bm.close()


class TestCheckpointLoggingAndSnapshots:
    """Tests I, J, K, L, Q: Checkpoint records and snapshot structures."""

    def test_checkpoint_begin_and_end_logging(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Tests I & J: Create checkpoint, verify BEGIN and END records exist and link."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            # Create checkpoint
            info = cm.create_checkpoint()

            assert info.begin_lsn == 1
            assert info.end_lsn == 2
            assert info.end_lsn > info.begin_lsn

            # Verify records in WAL
            records = lm.read_all()
            assert len(records) == 2

            rec_begin = records[0]
            assert rec_begin.lsn == 1
            assert rec_begin.record_type == LogRecordType.CHECKPOINT_BEGIN

            rec_end = records[1]
            assert rec_end.lsn == 2
            assert rec_end.record_type == LogRecordType.CHECKPOINT_END
            assert rec_end.checkpoint_begin_lsn == 1
            assert rec_end.prev_lsn == 1
            assert rec_end.checkpoint_payload is not None

            cm.close()
            bm.close()
            tm.close()

    def test_checkpoint_snapshot_contents(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Tests K, L, Q: Verify active transaction table, last_lsn, and DPT contents."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)
            bm = BufferManager(disk_manager=dm, pool_size=10, log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            # Allocate pages
            bm.allocate_page()  # Page 0
            bm.allocate_page()  # Page 1
            bm.allocate_page()  # Page 2

            # Begin transactions
            t1 = tm.begin_transaction()  # T1
            t2 = tm.begin_transaction()  # T2
            t3 = tm.begin_transaction()  # T3

            # T1 updates Page 0
            lsn_t1_1 = lm.append_update(t1.transaction_id, 0, 0, b"old1", b"new1")
            bm.set_page_lsn(0, lsn_t1_1)

            # T1 updates Page 0 again
            lsn_t1_2 = lm.append_update(t1.transaction_id, 0, 10, b"old2", b"new2")
            bm.set_page_lsn(0, lsn_t1_2)

            # T2 updates Page 1
            lsn_t2_1 = lm.append_update(t2.transaction_id, 1, 0, b"old3", b"new3")
            bm.set_page_lsn(1, lsn_t2_1)

            # Commit T3
            tm.commit_transaction(t3.transaction_id)

            # Create checkpoint
            info = cm.create_checkpoint()
            snapshot = info.snapshot

            # Check Transaction Table Snapshot
            tx_table = snapshot.transaction_table
            assert len(tx_table) == 3

            assert tx_table[1].transaction_id == 1
            assert tx_table[1].state == TransactionState.RUNNING
            assert tx_table[1].last_lsn == lsn_t1_2  # Most recent LSN for T1

            assert tx_table[2].transaction_id == 2
            assert tx_table[2].state == TransactionState.RUNNING
            assert tx_table[2].last_lsn == lsn_t2_1  # Most recent LSN for T2

            assert tx_table[3].transaction_id == 3
            assert tx_table[3].state == TransactionState.COMMITTED

            # Check Dirty Page Table Snapshot
            dpt_snap = snapshot.dirty_page_table
            assert len(dpt_snap) == 2
            assert dpt_snap[0] == lsn_t1_1  # recLSN is the FIRST update LSN!
            assert dpt_snap[1] == lsn_t2_1

            cm.close()
            bm.close()
            tm.close()


class TestCheckpointDurabilityAndDiscovery:
    """Tests M, N, O, P, R: Durability, reopen, discovery, immutability, corruption."""

    def test_checkpoint_durability_and_reopen(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test M: Checkpoints survive close and reopen."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            bm.allocate_page()
            t1 = tm.begin_transaction()
            lsn1 = lm.append_update(t1.transaction_id, 0, 0, b"a", b"b")
            bm.set_page_lsn(0, lsn1)

            checkpoint = cm.create_checkpoint()
            assert checkpoint.begin_lsn > 0

            cm.close()
            bm.close()
            tm.close()
            lm.close()
            dm.close()

        # Reopen managers
        with DiskManager(db_path=temp_db_path) as dm2, LogManager(wal_path=temp_wal_path) as lm2:
            tm2 = TransactionManager(log_manager=lm2)
            bm2 = BufferManager(disk_manager=dm2, pool_size=5, log_manager=lm2)
            cm2 = CheckpointManager(log_manager=lm2, transaction_manager=tm2, buffer_manager=bm2)

            latest = cm2.get_latest_checkpoint()
            assert latest is not None
            assert latest.begin_lsn == checkpoint.begin_lsn
            assert latest.end_lsn == checkpoint.end_lsn
            assert latest.snapshot.dirty_page_table == {0: lsn1}
            assert latest.snapshot.transaction_table[1].transaction_id == 1
            assert latest.snapshot.transaction_table[1].last_lsn == lsn1

            cm2.close()
            bm2.close()
            tm2.close()

    def test_latest_completed_checkpoint(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test N: Multiple checkpoints, verify newest completed checkpoint is returned."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            cp1 = cm.create_checkpoint()

            # Do some work
            t1 = tm.begin_transaction()
            lm.append_commit(t1.transaction_id)

            cp2 = cm.create_checkpoint()

            latest = cm.get_latest_checkpoint()
            assert latest is not None
            assert latest.begin_lsn == cp2.begin_lsn
            assert latest.end_lsn == cp2.end_lsn
            assert latest.end_lsn > cp1.end_lsn

            cm.close()
            bm.close()
            tm.close()

    def test_incomplete_checkpoint_handling(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test O: CHECKPOINT_BEGIN without CHECKPOINT_END is not considered completed."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            # Complete first checkpoint
            cp1 = cm.create_checkpoint()

            # Simulate crash midway through second checkpoint: write BEGIN only
            begin2_lsn = lm.append_checkpoint_begin()
            lm.flush()

            # Latest completed checkpoint must still be cp1, NOT the incomplete one!
            latest = cm.get_latest_checkpoint()
            assert latest is not None
            assert latest.begin_lsn == cp1.begin_lsn
            assert latest.end_lsn == cp1.end_lsn

            cm.close()
            bm.close()
            tm.close()

    def test_no_checkpoint_returns_none(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test discovery returns None when no checkpoints have been created."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            assert cm.get_latest_checkpoint() is None

            # Only write a standalone CHECKPOINT_BEGIN
            lm.append_checkpoint_begin()
            lm.flush()
            assert cm.get_latest_checkpoint() is None

            cm.close()
            bm.close()
            tm.close()

    def test_checkpoint_snapshot_immutability(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test P: Modifying active transaction/DPT state post-checkpoint leaves snapshot unchanged."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            bm.allocate_page()  # Page 0
            t1 = tm.begin_transaction()
            lsn1 = lm.append_update(t1.transaction_id, 0, 0, b"old", b"new")
            bm.set_page_lsn(0, lsn1)

            # Create checkpoint
            info = cm.create_checkpoint()
            snapshot = info.snapshot

            assert snapshot.dirty_page_table == {0: lsn1}
            assert snapshot.transaction_table[1].state == TransactionState.RUNNING

            # Mutate live system state after checkpoint
            bm.flush_page(0)  # DPT cleared for page 0
            tm.commit_transaction(t1.transaction_id)  # T1 committed

            assert not bm.dirty_page_table.is_dirty(0)
            assert t1.is_committed

            # Old snapshot MUST remain unchanged!
            assert snapshot.dirty_page_table == {0: lsn1}
            assert snapshot.transaction_table[1].state == TransactionState.RUNNING

            # Deserialized snapshot from WAL must also match the historical state
            from_wal = cm.get_latest_checkpoint()
            assert from_wal is not None
            assert from_wal.snapshot.dirty_page_table == {0: lsn1}
            assert from_wal.snapshot.transaction_table[1].state == TransactionState.RUNNING

            cm.close()
            bm.close()
            tm.close()

    def test_checkpoint_corruption_handling(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test R: Corrupted checkpoint payload raises CheckpointCorruptionError."""
        # Directly test snapshot deserialization corruption
        with pytest.raises(CheckpointCorruptionError):
            CheckpointSnapshot.deserialize(b"corrupt_data")

        with pytest.raises(CheckpointCorruptionError):
            # Bad magic
            CheckpointSnapshot.deserialize(b"BADM\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00")

        with pytest.raises(CheckpointCorruptionError):
            # Truncated data
            CheckpointSnapshot.deserialize(b"CPSS")

    def test_closed_checkpoint_manager_raises(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test operations on closed CheckpointManager raise CheckpointManagerClosedError."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)
            cm.close()

            assert cm.is_closed
            with pytest.raises(CheckpointManagerClosedError):
                cm.create_checkpoint()
            with pytest.raises(CheckpointManagerClosedError):
                cm.get_latest_checkpoint()

    def test_checkpoint_snapshot_dict_roundtrip(self) -> None:
        """Test CheckpointSnapshot to_dict and from_dict roundtrip."""
        dpt = {1: 10, 2: 20}
        tx_table = {
            100: TransactionTableEntry(100, TransactionState.RUNNING, 10),
            200: TransactionTableEntry(200, TransactionState.COMMITTED, 25),
        }
        snapshot = CheckpointSnapshot(transaction_table=tx_table, dirty_page_table=dpt)
        d = snapshot.to_dict()
        restored = CheckpointSnapshot.from_dict(d)
        assert restored == snapshot
        assert restored.serialize() == snapshot.serialize()

    def test_dpt_iteration_and_keyerror(self) -> None:
        """Test DirtyPageTable iteration and KeyError on missing key."""
        dpt = DirtyPageTable({2: 20, 1: 10})
        assert list(dpt) == [(1, 10), (2, 20)]
        assert dpt[1] == 10
        with pytest.raises(KeyError):
            _ = dpt[99]

    def test_checkpoint_info_validation(self) -> None:
        """Test CheckpointInfo parameter validation."""
        snapshot = CheckpointSnapshot()
        with pytest.raises(InvalidCheckpointError):
            CheckpointInfo(begin_lsn=0, end_lsn=2, snapshot=snapshot)
        with pytest.raises(InvalidCheckpointError):
            CheckpointInfo(begin_lsn=2, end_lsn=2, snapshot=snapshot)  # end_lsn must be > begin_lsn
        with pytest.raises(InvalidCheckpointError):
            CheckpointInfo(begin_lsn=5, end_lsn=3, snapshot=snapshot)

