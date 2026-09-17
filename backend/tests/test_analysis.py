"""Comprehensive test suite for Phase 7: ARIES Analysis Phase."""

from pathlib import Path
import pytest

from app.buffer.buffer_manager import BufferManager
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.recovery.analysis import Analysis
from app.recovery.exceptions import AnalysisError
from app.recovery.recovery_state import AnalysisResult, RecoveredTransaction
from app.storage.disk_manager import DiskManager
from app.transaction.transaction import TransactionState
from app.transaction.transaction_manager import TransactionManager
from app.wal.log_manager import LogManager


@pytest.fixture
def temp_wal_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary WAL filepath."""
    return tmp_path / "test_analysis.wal"


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary DB filepath."""
    return tmp_path / "test_analysis.db"


class TestARIESAnalysis:
    """Comprehensive test scenarios for ARIES Analysis recovery phase."""

    def test_analysis_no_checkpoint(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 1: No checkpoint present — scan entire WAL from beginning.

        Scenario:
            BEGIN T1 (LSN 1)
            UPDATE T1 P1 (LSN 2)
            COMMIT T1 (LSN 3)
            BEGIN T2 (LSN 4)
            UPDATE T2 P2 (LSN 5)
            [CRASH]
        """
        with LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)

            t1 = tm.begin_transaction()  # LSN 1 (BEGIN T1)
            lsn_u1 = lm.append_update(t1.transaction_id, page_id=1, offset=0, before_image=b"a", after_image=b"b")  # LSN 2
            tm.commit_transaction(t1.transaction_id)  # LSN 3 (COMMIT T1)

            t2 = tm.begin_transaction()  # LSN 4 (BEGIN T2)
            lsn_u2 = lm.append_update(t2.transaction_id, page_id=2, offset=0, before_image=b"c", after_image=b"d")  # LSN 5
            lm.flush()

        # Run Analysis on persistent WAL
        with LogManager(wal_path=temp_wal_path) as lm_recovery:
            result = Analysis.analyze(log_manager=lm_recovery)

            # Winners & Losers
            assert result.winners == [1]
            assert result.losers == [2]

            # Transaction Table
            tt = result.transaction_table
            assert len(tt) == 2
            assert tt[1].state == TransactionState.COMMITTED
            assert tt[1].last_lsn == 3
            assert tt[2].state == TransactionState.RUNNING
            assert tt[2].last_lsn == 5

            # Dirty Page Table
            dpt = result.dirty_page_table
            assert len(dpt) == 2
            assert dpt[1] == lsn_u1  # LSN 2
            assert dpt[2] == lsn_u2  # LSN 5

            # Redo Start LSN
            assert result.redo_start_lsn == lsn_u1  # min(2, 5) = 2
            assert result.analysis_start_lsn == 1
            assert result.last_analyzed_lsn == 5
            assert result.checkpoint_info is None

    def test_analysis_checkpoint_with_subsequent_activity(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 2: Checkpoint present with subsequent activity before crash.

        Scenario:
            BEGIN T1 (LSN 1)
            UPDATE T1 P1 (LSN 2)
            CHECKPOINT [BEGIN LSN 3, END LSN 4] (Snapshot: T1 running @ LSN 2, DPT: P1 -> 2)
            UPDATE T1 P2 (LSN 5)
            COMMIT T1 (LSN 6)
            BEGIN T2 (LSN 7)
            UPDATE T2 P3 (LSN 8)
            [CRASH]
        """
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=10, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            bm.allocate_page()  # P0
            bm.allocate_page()  # P1
            bm.allocate_page()  # P2
            bm.allocate_page()  # P3

            t1 = tm.begin_transaction()  # LSN 1
            lsn1 = lm.append_update(t1.transaction_id, 1, 0, b"a", b"b")  # LSN 2
            bm.set_page_lsn(1, lsn1)

            # Create Checkpoint (LSN 3, 4)
            cp = cm.create_checkpoint()
            assert cp.begin_lsn == 3
            assert cp.end_lsn == 4

            # Subsequent activity
            lsn2 = lm.append_update(t1.transaction_id, 2, 0, b"c", b"d")  # LSN 5
            bm.set_page_lsn(2, lsn2)
            tm.commit_transaction(t1.transaction_id)  # LSN 6

            t2 = tm.begin_transaction()  # LSN 7
            lsn3 = lm.append_update(t2.transaction_id, 3, 0, b"e", b"f")  # LSN 8
            bm.set_page_lsn(3, lsn3)
            lm.flush()

        # Reopen only WAL and run Analysis
        with LogManager(wal_path=temp_wal_path) as lm_recovery:
            analysis = Analysis(log_manager=lm_recovery)
            result = analysis.run()

            # Winners & Losers
            assert result.winners == [1]
            assert result.losers == [2]

            # Transaction Table
            tt = result.transaction_table
            assert len(tt) == 2
            assert tt[1].state == TransactionState.COMMITTED
            assert tt[1].last_lsn == 6
            assert tt[2].state == TransactionState.RUNNING
            assert tt[2].last_lsn == 8

            # Dirty Page Table: P1 (from checkpoint), P2, P3 (after checkpoint)
            dpt = result.dirty_page_table
            assert len(dpt) == 3
            assert dpt[1] == 2  # from checkpoint snapshot
            assert dpt[2] == 5  # from post-checkpoint update
            assert dpt[3] == 8  # from post-checkpoint update

            # Redo Start LSN = min(2, 5, 8) = 2
            assert result.redo_start_lsn == 2
            assert result.analysis_start_lsn == 3  # Checkpoint BEGIN LSN
            assert result.last_analyzed_lsn == 8
            assert result.checkpoint_info is not None
            assert result.checkpoint_info.begin_lsn == 3

    def test_analysis_incomplete_checkpoint_fallback(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 3: Incomplete checkpoint (BEGIN without END) is ignored; falls back properly."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            bm.allocate_page()  # Page 0
            bm.allocate_page()  # Page 1

            # Valid completed checkpoint 1
            t1 = tm.begin_transaction()  # LSN 1
            lsn1 = lm.append_update(t1.transaction_id, 0, 0, b"a", b"b")  # LSN 2
            bm.set_page_lsn(0, lsn1)
            cp1 = cm.create_checkpoint()  # LSN 3 (BEGIN), LSN 4 (END)

            # Simulate crash midway through checkpoint 2 (write BEGIN only)
            lm.append_checkpoint_begin()  # LSN 5 (incomplete checkpoint)
            t2 = tm.begin_transaction()  # LSN 6
            lsn2 = lm.append_update(t2.transaction_id, 1, 0, b"c", b"d")  # LSN 7
            bm.set_page_lsn(1, lsn2)
            lm.flush()

        with LogManager(wal_path=temp_wal_path) as lm_recovery:
            result = Analysis.analyze(log_manager=lm_recovery)

            # Checkpoint 1 should have been used, not the incomplete checkpoint at LSN 5
            assert result.checkpoint_info is not None
            assert result.checkpoint_info.begin_lsn == cp1.begin_lsn

            # T1 was running at cp1, T2 started after
            assert result.losers == [1, 2]
            assert result.winners == []
            assert result.dirty_page_table == {0: lsn1, 1: lsn2}
            assert result.redo_start_lsn == lsn1

    def test_analysis_multiple_interleaved_transactions(self, temp_wal_path: Path) -> None:
        """Test 4: Multiple interleaved transactions with commits and ongoing updates.

        Scenario:
            BEGIN T1 (LSN 1)
            BEGIN T2 (LSN 2)
            UPDATE T1 P1 (LSN 3)
            UPDATE T2 P2 (LSN 4)
            COMMIT T1 (LSN 5)
            UPDATE T2 P3 (LSN 6)
            [CRASH]
        """
        with LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)

            t1 = tm.begin_transaction()  # LSN 1
            t2 = tm.begin_transaction()  # LSN 2
            lm.append_update(t1.transaction_id, 1, 0, b"1", b"2")  # LSN 3
            lm.append_update(t2.transaction_id, 2, 0, b"3", b"4")  # LSN 4
            tm.commit_transaction(t1.transaction_id)  # LSN 5
            lm.append_update(t2.transaction_id, 3, 0, b"5", b"6")  # LSN 6
            lm.flush()

        with LogManager(wal_path=temp_wal_path) as lm_recovery:
            result = Analysis.analyze(log_manager=lm_recovery)

            assert result.winners == [1]
            assert result.losers == [2]

            assert result.transaction_table[1].last_lsn == 5
            assert result.transaction_table[2].last_lsn == 6
            assert result.dirty_page_table == {1: 3, 2: 4, 3: 6}
            assert result.redo_start_lsn == 3

    def test_analysis_repeated_updates_to_same_page(self, temp_wal_path: Path) -> None:
        """Test 5: Repeated updates to the same page preserve the FIRST update's recLSN.

        Scenario:
            BEGIN T1 (LSN 1)
            UPDATE T1 P1 (LSN 2) -> recLSN = 2
            UPDATE T1 P1 (LSN 3) -> recLSN must stay 2!
            UPDATE T1 P1 (LSN 4) -> recLSN must stay 2!
            [CRASH]
        """
        with LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)
            t1 = tm.begin_transaction()  # LSN 1
            lm.append_update(t1.transaction_id, 1, 0, b"a", b"b")  # LSN 2
            lm.append_update(t1.transaction_id, 1, 10, b"c", b"d")  # LSN 3
            lm.append_update(t1.transaction_id, 1, 20, b"e", b"f")  # LSN 4
            lm.flush()

        with LogManager(wal_path=temp_wal_path) as lm_recovery:
            result = Analysis.analyze(log_manager=lm_recovery)

            assert result.dirty_page_table == {1: 2}
            assert result.redo_start_lsn == 2
            assert result.transaction_table[1].last_lsn == 4
            assert result.losers == [1]

    def test_analysis_empty_dpt_redo_start_lsn_none(self, temp_wal_path: Path) -> None:
        """Test 6: When DPT is empty, redo_start_lsn is None.

        Scenario:
            BEGIN T1 (LSN 1)
            COMMIT T1 (LSN 2) (no page modifications)
            [CRASH]
        """
        with LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)
            t1 = tm.begin_transaction()
            tm.commit_transaction(t1.transaction_id)
            lm.flush()

        with LogManager(wal_path=temp_wal_path) as lm_recovery:
            result = Analysis.analyze(log_manager=lm_recovery)

            assert result.dirty_page_table == {}
            assert result.redo_start_lsn is None
            assert result.winners == [1]
            assert result.losers == []

    def test_analysis_restart_independence(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 7: Analysis reconstructs state strictly from persistent files after restart."""
        # Initial run
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            bm.allocate_page()
            t1 = tm.begin_transaction()
            lsn1 = lm.append_update(t1.transaction_id, 0, 0, b"x", b"y")
            bm.set_page_lsn(0, lsn1)
            cm.create_checkpoint()

            t2 = tm.begin_transaction()
            lsn2 = lm.append_update(t2.transaction_id, 0, 10, b"y", b"z")
            lm.flush()

            # Fully close all managers to wipe out memory
            cm.close()
            bm.close()
            tm.close()
            lm.close()
            dm.close()

        # Independent process simulation: only open LogManager on the WAL file
        with LogManager(wal_path=temp_wal_path) as lm_reopened:
            result = Analysis.analyze(log_manager=lm_reopened)

            assert result.winners == []
            assert result.losers == [1, 2]
            assert result.dirty_page_table[0] == lsn1
            assert result.redo_start_lsn == lsn1

    def test_analysis_aborted_transactions_handling(self, temp_wal_path: Path) -> None:
        """Test 8: Aborted transactions are not classified as active losers."""
        with LogManager(wal_path=temp_wal_path) as lm:
            tm = TransactionManager(log_manager=lm)

            t1 = tm.begin_transaction()  # LSN 1
            lsn1 = lm.append_update(t1.transaction_id, 1, 0, b"a", b"b")  # LSN 2
            tm.abort_transaction(t1.transaction_id)  # LSN 3

            t2 = tm.begin_transaction()  # LSN 4
            lsn2 = lm.append_update(t2.transaction_id, 2, 0, b"c", b"d")  # LSN 5
            lm.flush()

        with LogManager(wal_path=temp_wal_path) as lm_recovery:
            result = Analysis.analyze(log_manager=lm_recovery)

            # T1 is ABORTED (neither winner nor active loser)
            # T2 is RUNNING (loser)
            assert result.winners == []
            assert result.losers == [2]

            assert result.transaction_table[1].state == TransactionState.ABORTED
            assert result.transaction_table[1].last_lsn == 3
            assert result.transaction_table[2].state == TransactionState.RUNNING
            assert result.transaction_table[2].last_lsn == 5

    def test_analysis_empty_wal(self, temp_wal_path: Path) -> None:
        """Test 9: Analysis on an empty WAL file returns empty result cleanly."""
        with LogManager(wal_path=temp_wal_path) as lm:
            result = Analysis.analyze(log_manager=lm)

            assert result.transaction_table == {}
            assert result.dirty_page_table == {}
            assert result.winners == []
            assert result.losers == []
            assert result.redo_start_lsn is None
            assert result.analysis_start_lsn is None
            assert result.last_analyzed_lsn is None
            assert result.checkpoint_info is None

    def test_analysis_result_serialization_and_immutability(self) -> None:
        """Test 10: AnalysisResult to_dict and immutability."""
        tx1 = RecoveredTransaction(1, TransactionState.COMMITTED, 10)
        tx2 = RecoveredTransaction(2, TransactionState.RUNNING, 20)
        tt = {1: tx1, 2: tx2}
        dpt = {0: 5, 1: 15}

        result = AnalysisResult(
            transaction_table=tt,
            dirty_page_table=dpt,
            winners=[1],
            losers=[2],
            redo_start_lsn=5,
            analysis_start_lsn=1,
            last_analyzed_lsn=20,
        )

        d = result.to_dict()
        assert d["winners"] == [1]
        assert d["losers"] == [2]
        assert d["redo_start_lsn"] == 5
        assert d["dirty_page_table"] == {0: 5, 1: 15}

        # Modifying original dicts must not affect AnalysisResult
        tt[3] = RecoveredTransaction(3, TransactionState.RUNNING, 30)
        dpt[2] = 25
        assert len(result.transaction_table) == 2
        assert len(result.dirty_page_table) == 2

    def test_analysis_initialization_validation(self) -> None:
        """Test 11: Analysis initialization validates parameters."""
        with pytest.raises(AnalysisError):
            Analysis(log_manager=None)  # type: ignore

    def test_analysis_does_not_modify_disk_or_wal(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Test 12: Analysis is strictly read-only and leaves disk and WAL files unmodified."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            t1 = tm.begin_transaction()
            lsn1 = lm.append_update(t1.transaction_id, 0, 0, b"abc", b"xyz")
            bm.set_page_lsn(0, lsn1)
            lm.flush()

            bm.close()
            tm.close()
            lm.close()
            dm.close()

        # Capture file bytes before analysis
        db_bytes_before = temp_db_path.read_bytes()
        wal_bytes_before = temp_wal_path.read_bytes()

        # Execute analysis
        with LogManager(wal_path=temp_wal_path) as lm_recovery:
            res = Analysis.analyze(log_manager=lm_recovery)
            assert res.losers == [1]

        # Verify files are bitwise identical
        assert temp_db_path.read_bytes() == db_bytes_before
        assert temp_wal_path.read_bytes() == wal_bytes_before

