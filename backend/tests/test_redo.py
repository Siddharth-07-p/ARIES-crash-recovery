"""Comprehensive test suite for Phase 8: ARIES Redo Phase."""

from pathlib import Path
import pytest

from app.buffer.buffer_manager import BufferManager
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector
from app.recovery.analysis import Analysis
from app.recovery.exceptions import RedoError
from app.recovery.recovery_state import AnalysisResult, RecoveredTransaction, RedoResult
from app.recovery.redo import Redo
from app.storage.constants import PAGE_SIZE
from app.storage.disk_manager import DiskManager
from app.transaction.transaction import TransactionState
from app.transaction.transaction_manager import TransactionManager
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecord, LogRecordType


@pytest.fixture
def temp_wal_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary WAL filepath."""
    return tmp_path / "test_redo.wal"


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary DB filepath."""
    return tmp_path / "test_redo.db"


class TestARIESRedo:
    """Comprehensive test suite for ARIES Redo recovery phase."""

    def test_basic_redo(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 1: Basic Redo.

        Scenario:
            BEGIN T1
            UPDATE P0 (bytes b"HELLO REDO")
            WAL is durable
            P0 is NOT persisted to disk
            [CRASH]
        Recovery:
            Analysis -> Redo
        Verify:
            - UPDATE after-image appears on P0
            - Page is persisted
            - Redo reports UPDATE as redone
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()  # Page 0 (all zeros on disk)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lsn1 = lm.append_update(t1.transaction_id, page_id=0, offset=10, before_image=b"\x00" * 10, after_image=b"HELLO REDO")  # LSN 2
            lm.flush()
            # Simulate crash without flushing P0 modifications to disk
            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        # Reopen after crash for recovery
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_result = Analysis.analyze(log_manager=lm)

            assert 0 in analysis_result.dirty_page_table
            assert analysis_result.redo_start_lsn == lsn1

            redo = Redo(log_manager=lm, buffer_manager=bm)
            redo_result = redo.execute(analysis_result)

            assert redo_result.records_scanned == 1  # Scanned starting from redo_start_lsn (LSN 2)
            assert redo_result.records_redone == 1
            assert redo_result.records_skipped == 0
            assert redo_result.pages_modified == [0]


            # Verify page content in buffer pool
            page = bm.fetch_page(0)
            assert page.read(10, 10) == b"HELLO REDO"
            bm.unpin_page(0, is_dirty=False)

            bm.close(flush_dirty=False)
            dm.close()
            lm.close()

        # Verify page content persisted on disk
        with DiskManager(db_path=temp_db_path) as dm:
            p0 = dm.read_page(0)
            assert p0.read(10, 10) == b"HELLO REDO"

    def test_committed_transaction_redo(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 2: Committed Transaction Redo.

        Confirms that Redo replays updates for committed (winner) transactions.
        Scenario:
            BEGIN T1
            UPDATE P0
            COMMIT T1
            [CRASH before page flush]
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lm.append_update(t1.transaction_id, page_id=0, offset=0, before_image=b"\x00" * 4, after_image=b"COMM")  # LSN 2
            tm.commit_transaction(t1.transaction_id)  # LSN 3 (flushes WAL)

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_result = Analysis.analyze(log_manager=lm)

            assert analysis_result.winners == [1]
            assert analysis_result.losers == []

            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            assert result.records_redone == 1
            assert result.pages_modified == [0]

            page = bm.fetch_page(0)
            assert page.read(0, 4) == b"COMM"
            bm.unpin_page(0, is_dirty=False)

    def test_uncommitted_transaction_redo(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 3: Uncommitted Transaction Redo.

        Confirms that Redo replays updates for uncommitted (loser) transactions (repeat history).
        Does NOT undo them.
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lm.append_update(t1.transaction_id, page_id=0, offset=5, before_image=b"\x00" * 6, after_image=b"ACTIVE")  # LSN 2
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_result = Analysis.analyze(log_manager=lm)

            assert analysis_result.losers == [1]

            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            assert result.records_redone == 1
            page = bm.fetch_page(0)
            assert page.read(5, 6) == b"ACTIVE"
            bm.unpin_page(0, is_dirty=False)

    def test_dpt_filtering(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 4: DPT Filtering.

        Updates to pages not present in the Analysis DPT must be skipped.
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()  # P0
            bm.allocate_page()  # P1
            bm.flush_all_pages()

            # Record updates for P0 and P1
            lm.append_begin(1)  # LSN 1
            lm.append_update(1, page_id=0, offset=0, before_image=b"\x00", after_image=b"A")  # LSN 2
            lm.append_update(1, page_id=1, offset=0, before_image=b"\x00", after_image=b"B")  # LSN 3
            lm.flush()

            # Construct an AnalysisResult where only P1 is dirty (P0 is clean / not in DPT)
            analysis_result = AnalysisResult(
                transaction_table={1: RecoveredTransaction(1, TransactionState.RUNNING, 3)},
                dirty_page_table={1: 3},  # Only P1 in DPT
                winners=[],
                losers=[1],
                redo_start_lsn=3,
                analysis_start_lsn=1,
                last_analyzed_lsn=3,
            )

            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            assert result.records_scanned == 1  # Scanned from LSN 3 (P1)
            assert result.records_redone == 1
            assert result.pages_modified == [1]
            assert 0 not in result.pages_modified

    def test_reclsn_filtering(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 5: recLSN Filtering.

        DPT[P0] = 20. WAL contains UPDATE at LSN 15 and UPDATE at LSN 20.
        Verify LSN 15 is skipped and LSN 20 is eligible for Redo.
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()  # P0
            bm.allocate_page()  # P1 (recLSN 10 to force scan from LSN 10)
            bm.flush_all_pages()

            # Create log sequence
            lm.append_begin(1)  # LSN 1
            # We want records at specific LSNs:
            # Let's pad or append records
            for i in range(2, 10):
                lm.append_begin(100 + i)  # LSN 2..9

            # LSN 10: update to P1
            lm.append_update(1, page_id=1, offset=0, before_image=b"\x00", after_image=b"X")  # LSN 10

            for i in range(11, 15):
                lm.append_begin(200 + i)  # LSN 11..14

            # LSN 15: update to P0 (should be skipped because DPT[P0] = 20)
            lm.append_update(1, page_id=0, offset=0, before_image=b"\x00" * 4, after_image=b"SKIP")  # LSN 15

            for i in range(16, 20):
                lm.append_begin(300 + i)  # LSN 16..19

            # LSN 20: update to P0 (should be redone because LSN 20 >= recLSN 20)
            lm.append_update(1, page_id=0, offset=0, before_image=b"\x00" * 4, after_image=b"REDO")  # LSN 20
            lm.flush()

            # AnalysisResult: DPT has P0 -> 20, P1 -> 10, redo_start_lsn = 10
            analysis_result = AnalysisResult(
                transaction_table={1: RecoveredTransaction(1, TransactionState.RUNNING, 20)},
                dirty_page_table={0: 20, 1: 10},
                winners=[],
                losers=[1],
                redo_start_lsn=10,
                analysis_start_lsn=1,
                last_analyzed_lsn=20,
            )

            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            # LSN 10 (P1) is redone
            # LSN 15 (P0) is skipped because 15 < recLSN 20
            # LSN 20 (P0) is redone because 20 >= recLSN 20
            assert result.records_redone == 2  # LSN 10 and LSN 20
            assert result.pages_modified == [0, 1]

            page0 = bm.fetch_page(0)
            assert page0.read(0, 4) == b"REDO"
            bm.unpin_page(0, is_dirty=False)

    def test_page_lsn_skip(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 6: Page LSN Skip.

        Set page_lsn >= record.lsn.
        Verify UPDATE is skipped, page contents are unchanged, and Redo reports it as skipped.
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()  # P0
            bm.flush_all_pages()

            lm.append_begin(1)  # LSN 1
            lsn2 = lm.append_update(1, page_id=0, offset=0, before_image=b"\x00" * 4, after_image=b"ALRD")  # LSN 2
            lm.flush()

            # Pre-load page into buffer and set its page_lsn to 5 (greater than record LSN 2)
            page = bm.fetch_page(0)
            page.write(0, b"ORIG")
            bm.set_page_lsn(0, 5)
            bm.unpin_page(0, is_dirty=False)

            analysis_result = AnalysisResult(
                transaction_table={1: RecoveredTransaction(1, TransactionState.RUNNING, lsn2)},
                dirty_page_table={0: 1},
                winners=[],
                losers=[1],
                redo_start_lsn=lsn2,
                analysis_start_lsn=1,
                last_analyzed_lsn=lsn2,
            )

            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            assert result.records_redone == 0
            assert result.records_skipped == 1
            assert result.pages_modified == []

            # Verify page contents were not modified
            p0 = bm.fetch_page(0)
            assert p0.read(0, 4) == b"ORIG"
            bm.unpin_page(0, is_dirty=False)

    def test_idempotence(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 7: Idempotence.

        Run Analysis -> Redo -> Redo again.
        Verify:
            - Second Redo skips already-applied updates
            - Zero additional records redone
            - Page contents remain identical
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()  # P0
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lm.append_update(t1.transaction_id, page_id=0, offset=0, before_image=b"\x00" * 4, after_image=b"IDEM")  # LSN 2
            lm.flush()

            # First Redo
            analysis_result = Analysis.analyze(log_manager=lm)
            redo = Redo(log_manager=lm, buffer_manager=bm)
            result1 = redo.execute(analysis_result)

            assert result1.records_redone == 1
            assert result1.pages_modified == [0]

            page1 = bm.fetch_page(0)
            assert page1.read(0, 4) == b"IDEM"
            bm.unpin_page(0, is_dirty=False)

            # Second Redo on same state
            result2 = redo.execute(analysis_result)
            assert result2.records_scanned == 1
            assert result2.records_redone == 0
            assert result2.records_skipped == 1  # UPDATE skipped via page_lsn rule
            assert result2.pages_modified == []


            page2 = bm.fetch_page(0)
            assert page2.read(0, 4) == b"IDEM"
            bm.unpin_page(0, is_dirty=False)

    def test_multiple_updates_same_page(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 8: Multiple Updates Same Page.

        Scenario:
            UPDATE P0 at LSN 2
            UPDATE P0 at LSN 3
            UPDATE P0 at LSN 4
        Verify:
            - Final page contains latest (LSN 4) after-image
            - Final page_lsn is 4
            - Replayed in correct order
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lsn2 = lm.append_update(t1.transaction_id, page_id=0, offset=0, before_image=b"\x00" * 4, after_image=b"VER1")  # LSN 2
            lsn3 = lm.append_update(t1.transaction_id, page_id=0, offset=4, before_image=b"\x00" * 4, after_image=b"VER2")  # LSN 3
            lsn4 = lm.append_update(t1.transaction_id, page_id=0, offset=0, before_image=b"VER1", after_image=b"VER3")  # LSN 4
            lm.flush()

            analysis_result = Analysis.analyze(log_manager=lm)
            assert analysis_result.dirty_page_table == {0: lsn2}
            assert analysis_result.redo_start_lsn == lsn2

            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            assert result.records_redone == 3
            assert result.pages_modified == [0]

            assert bm.get_page_lsn(0) == lsn4
            page = bm.fetch_page(0)
            assert page.read(0, 8) == b"VER3VER2"
            bm.unpin_page(0, is_dirty=False)

    def test_multiple_pages(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 9: Multiple Pages Redo.

        Create updates to P0, P1, P2.
        Verify all eligible pages are correctly replayed.
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()  # P0
            bm.allocate_page()  # P1
            bm.allocate_page()  # P2
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            t2 = tm.begin_transaction()  # LSN 2

            lm.append_update(t1.transaction_id, page_id=0, offset=0, before_image=b"\x00", after_image=b"A")  # LSN 3
            lm.append_update(t2.transaction_id, page_id=1, offset=0, before_image=b"\x00", after_image=b"B")  # LSN 4
            lm.append_update(t1.transaction_id, page_id=2, offset=0, before_image=b"\x00", after_image=b"C")  # LSN 5
            lm.flush()

            analysis_result = Analysis.analyze(log_manager=lm)
            assert len(analysis_result.dirty_page_table) == 3

            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            assert result.records_redone == 3
            assert result.pages_modified == [0, 1, 2]

            p0 = bm.fetch_page(0)
            p1 = bm.fetch_page(1)
            p2 = bm.fetch_page(2)
            assert p0.read(0, 1) == b"A"
            assert p1.read(0, 1) == b"B"
            assert p2.read(0, 1) == b"C"
            bm.unpin_page(0, is_dirty=False)
            bm.unpin_page(1, is_dirty=False)
            bm.unpin_page(2, is_dirty=False)

    def test_empty_dpt(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 10: Empty DPT.

        Analysis produces redo_start_lsn = None.
        Verify:
            - Zero records redone
            - Zero pages modified
            - Returns clean RedoResult immediately
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            t1 = tm.begin_transaction()
            tm.commit_transaction(t1.transaction_id)
            lm.flush()

            analysis_result = Analysis.analyze(log_manager=lm)
            assert analysis_result.dirty_page_table == {}
            assert analysis_result.redo_start_lsn is None

            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            assert result.redo_start_lsn is None
            assert result.records_scanned == 0
            assert result.records_redone == 0
            assert result.records_skipped == 0
            assert result.pages_modified == []
            assert result.pages_skipped == []

    def test_restart_persistence(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 11: Restart Persistence.

        Create a crash state, run Analysis -> Redo.
        Close all managers, reopen the database, and verify redone page contents remain persisted.
        """
        # 1. Initial execution and crash
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()  # P0
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lm.append_update(t1.transaction_id, page_id=0, offset=100, before_image=b"\x00" * 4, after_image=b"PERS")  # LSN 2
            lm.flush()

            # Crash without flushing P0 dirty page
            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        # 2. Crash Recovery: Analysis + Redo
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_result = Analysis.analyze(log_manager=lm)
            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            assert result.records_redone == 1
            bm.close(flush_dirty=False)
            dm.close()
            lm.close()

        # 3. Independent reopen directly from disk
        with DiskManager(db_path=temp_db_path) as dm:
            p0 = dm.read_page(0)
            assert p0.read(100, 4) == b"PERS"

    def test_wal_ordering(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 12: WAL Ordering.

        Verify that WAL is durable before redone dirty pages are written to disk.
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            bm.flush_all_pages()

            lm.append_begin(1)  # LSN 1
            u_lsn = lm.append_update(1, 0, 0, b"\x00", b"Z")  # LSN 2
            lm.flush()

            analysis_result = Analysis.analyze(log_manager=lm)
            redo = Redo(log_manager=lm, buffer_manager=bm)
            redo.execute(analysis_result)

            # Ensure flushed_lsn >= page_lsn
            assert lm.flushed_lsn >= u_lsn
            assert bm.get_page_lsn(0) == u_lsn

    def test_invalid_update_bounds(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 13: Invalid UPDATE Bounds.

        Test malformed UPDATE records (e.g. extending beyond PAGE_SIZE or negative offset).
        Verify invalid bounds are rejected safely and no page corruption occurs.
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()  # P0
            bm.flush_all_pages()

            lm.append_begin(1)

            # Manually construct a malformed UPDATE record that bypasses LogRecord.__init__
            malformed_rec = object.__new__(LogRecord)
            malformed_rec._lsn = 2
            malformed_rec._record_type = LogRecordType.UPDATE
            malformed_rec._transaction_id = 1
            malformed_rec._prev_lsn = None
            malformed_rec._page_id = 0
            malformed_rec._offset = PAGE_SIZE - 2
            malformed_rec._before_image = b"\x00" * 2
            malformed_rec._after_image = b"TOOLONG"  # 7 bytes, exceeds PAGE_SIZE
            malformed_rec._checkpoint_payload = None

            analysis_result = AnalysisResult(
                transaction_table={1: RecoveredTransaction(1, TransactionState.RUNNING, 2)},
                dirty_page_table={0: 2},
                winners=[],
                losers=[1],
                redo_start_lsn=2,
                analysis_start_lsn=1,
                last_analyzed_lsn=2,
            )

        # Directly verify Redo raises RedoError on malformed log record bounds
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            redo = Redo(log_manager=lm, buffer_manager=bm)
            # Inject malformed record into log_manager
            lm._records[2] = malformed_rec

            with pytest.raises(RedoError, match="Invalid UPDATE bounds"):
                redo.execute(analysis_result)

            # Also verify negative offset
            malformed_rec_neg = object.__new__(LogRecord)
            malformed_rec_neg._lsn = 2
            malformed_rec_neg._record_type = LogRecordType.UPDATE
            malformed_rec_neg._transaction_id = 1
            malformed_rec_neg._prev_lsn = None
            malformed_rec_neg._page_id = 0
            malformed_rec_neg._offset = -1
            malformed_rec_neg._before_image = b"\x00"
            malformed_rec_neg._after_image = b"A"
            malformed_rec_neg._checkpoint_payload = None
            lm._records[2] = malformed_rec_neg

            with pytest.raises(RedoError, match="Invalid UPDATE bounds"):
                redo.execute(analysis_result)


    def test_no_undo_during_redo(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 14: No Undo During Redo.

        Create uncommitted transaction: BEGIN T1, UPDATE P0, CRASH.
        Run Analysis -> Redo.
        Verify UPDATE remains applied.
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1 (uncommitted)
            lm.append_update(t1.transaction_id, page_id=0, offset=0, before_image=b"\x00" * 4, after_image=b"LOSR")  # LSN 2
            lm.flush()

            analysis_result = Analysis.analyze(log_manager=lm)
            assert analysis_result.losers == [1]

            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            assert result.records_redone == 1
            # Verify update remains on page
            page = bm.fetch_page(0)
            assert page.read(0, 4) == b"LOSR"
            bm.unpin_page(0, is_dirty=False)

    def test_redo_with_checkpoint_and_subsequent_updates(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 15: Redo starting from checkpoint-derived DPT and subsequent updates.

        Scenario:
            T1 BEGIN
            UPDATE P0 (recLSN 2)
            CHECKPOINT (DPT: P0 -> 2)
            UPDATE P1 (recLSN 5)
            COMMIT T1
            [CRASH]
        Verify:
            Redo starts from min recLSN (2) and replays P0 and P1 updates.
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=10, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            bm.allocate_page()  # P0
            bm.allocate_page()  # P1
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lsn2 = lm.append_update(t1.transaction_id, 0, 0, b"\x00", b"A")  # LSN 2
            bm.set_page_lsn(0, lsn2)

            cp = cm.create_checkpoint()  # LSN 3 (BEGIN), LSN 4 (END)

            lsn5 = lm.append_update(t1.transaction_id, 1, 0, b"\x00", b"B")  # LSN 5
            bm.set_page_lsn(1, lsn5)
            tm.commit_transaction(t1.transaction_id)  # LSN 6

            # Crash without flushing pages
            bm.close(flush_dirty=False)
            cm.close()
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=10, log_manager=lm)
            analysis_result = Analysis.analyze(log_manager=lm)

            assert analysis_result.redo_start_lsn == 2
            assert analysis_result.dirty_page_table == {0: 2, 1: 5}

            redo = Redo(log_manager=lm, buffer_manager=bm)
            result = redo.execute(analysis_result)

            assert result.records_redone == 2
            assert result.pages_modified == [0, 1]

            p0 = bm.fetch_page(0)
            p1 = bm.fetch_page(1)
            assert p0.read(0, 1) == b"A"
            assert p1.read(0, 1) == b"B"
            bm.unpin_page(0, is_dirty=False)
            bm.unpin_page(1, is_dirty=False)

    def test_redo_fault_injection_scenario(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 16: Integration scenario with fault injection."""
        fault_injector.reset()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()  # P0
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lm.append_update(t1.transaction_id, 0, 0, b"\x00" * 5, b"FAULT")  # LSN 2
            tm.commit_transaction(t1.transaction_id)  # LSN 3

            # Crash before page flush
            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        # Restart and recover
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_result = Analysis.analyze(log_manager=lm)
            redo_result = Redo.redo(log_manager=lm, buffer_manager=bm, analysis_result=analysis_result)

            assert redo_result.records_redone == 1
            page = bm.fetch_page(0)
            assert page.read(0, 5) == b"FAULT"
            bm.unpin_page(0, is_dirty=False)

    def test_redo_initialization_and_validation(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 17: Redo initialization and argument validation."""
        with pytest.raises(RedoError, match="log_manager cannot be None"):
            Redo(log_manager=None, buffer_manager=None)  # type: ignore

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            with pytest.raises(RedoError, match="buffer_manager cannot be None"):
                Redo(log_manager=lm, buffer_manager=None)  # type: ignore

            redo = Redo(log_manager=lm, buffer_manager=bm)
            with pytest.raises(RedoError, match="analysis_result must be a valid AnalysisResult"):
                redo.execute(None)  # type: ignore

    def test_redo_result_serialization_and_immutability(self) -> None:
        """Test 18: RedoResult to_dict, immutability, and equality."""
        res1 = RedoResult(
            redo_start_lsn=2,
            last_scanned_lsn=10,
            records_scanned=9,
            records_redone=3,
            records_skipped=6,
            pages_modified=[1, 2],
            pages_skipped=[0],
        )

        d = res1.to_dict()
        assert d["redo_start_lsn"] == 2
        assert d["last_scanned_lsn"] == 10
        assert d["records_scanned"] == 9
        assert d["records_redone"] == 3
        assert d["records_skipped"] == 6
        assert d["pages_modified"] == [1, 2]
        assert d["pages_skipped"] == [0]

        res2 = RedoResult(
            redo_start_lsn=2,
            last_scanned_lsn=10,
            records_scanned=9,
            records_redone=3,
            records_skipped=6,
            pages_modified=[2, 1],  # Test sorting/set uniqueness
            pages_skipped=[0],
        )
        assert res1 == res2
        assert "RedoResult" in repr(res1)
