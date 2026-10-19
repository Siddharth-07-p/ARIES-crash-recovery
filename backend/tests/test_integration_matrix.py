"""Comprehensive integration and correctness test matrix for Phase 11.

Covers all 20 required integration scenarios:
1. A clean database with no recovery work required.
2. A committed transaction whose updates must survive restart.
3. An incomplete transaction whose updates must be undone.
4. Multiple interleaved transactions with different commit/abort outcomes.
5. Multiple updates by the same transaction to the same page.
6. Multiple transactions updating different pages.
7. A transaction updating multiple pages before a crash.
8. Recovery with a checkpoint present.
9. Recovery when the checkpoint is absent or incomplete.
10. A crash during Redo followed by another recovery attempt.
11. A crash during Undo followed by another recovery attempt.
12. A crash after a CLR is flushed but before the corresponding page update is flushed.
13. A crash after Undo has completed but before recovery completion is recorded.
14. Repeated recovery on an already recovered database.
15. Corrupted or invalid WAL records and invalid LSN chains.
16. Invalid transaction or page references.
17. Recovery failures at each orchestration stage.
18. File-based recovery using the same database and WAL files across process restarts.
19. Buffer-pool pin/unpin safety and page consistency after recovery.
20. Compatibility with all existing public APIs and tests.
"""

import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional
import pytest

from app.buffer.buffer_manager import BufferManager
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.fault.constants import DEFAULT_CRASH_EXIT_CODE, FaultPoint
from app.fault.fault_injector import fault_injector
from app.recovery.analysis import Analysis
from app.recovery.exceptions import (
    AnalysisError,
    RecoveryError,
    RecoveryManagerError,
    RedoError,
    UndoError,
)
from app.recovery.recovery_manager import RecoveryManager
from app.recovery.recovery_state import (
    AnalysisResult,
    RecoveredTransaction,
    RecoveryResult,
    RedoResult,
    UndoResult,
)
from app.recovery.redo import Redo
from app.recovery.undo import Undo
from app.storage.constants import PAGE_SIZE
from app.storage.disk_manager import DiskManager
from app.storage.exceptions import PageNotFoundError
from app.transaction.transaction import TransactionState
from app.transaction.transaction_manager import TransactionManager
from app.wal.exceptions import LogCorruptionError
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecord, LogRecordType


@pytest.fixture
def temp_wal_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary WAL filepath."""
    return tmp_path / "test_matrix.wal"


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary DB filepath."""
    return tmp_path / "test_matrix.db"


def _run_subprocess_snippet(script_code: str, temp_dir: Path) -> subprocess.CompletedProcess:
    """Helper executing an isolated Python script in a separate subprocess."""
    backend_dir = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = backend_dir

    result = subprocess.run(
        [sys.executable, "-c", script_code],
        cwd=str(temp_dir),
        env=env,
        capture_output=True,
        text=True,
    )
    return result


class TestARIESIntegrationMatrix:
    """Comprehensive test matrix validating all 20 Phase 11 scenarios and ARIES correctness invariants."""

    # -------------------------------------------------------------------------
    # Scenario 1: Clean database with no recovery work required
    # -------------------------------------------------------------------------
    def test_scenario_01_clean_database_no_recovery_required(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 1: Recovery on an empty/clean database performs zero work and succeeds."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.stage == "COMPLETE"
            assert result.winners == []
            assert result.losers == []
            assert result.records_redone == 0
            assert result.clrs_generated == 0
            assert result.updates_undone == 0
            assert result.pages_modified == []
            assert rm.is_recovered is True

    # -------------------------------------------------------------------------
    # Scenario 2: Committed transaction whose updates must survive restart
    # -------------------------------------------------------------------------
    def test_scenario_02_committed_transaction_survives_restart(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 2: Committed transaction with dirty unflushed pages is redone and durable on restart."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            p0 = bm.allocate_page()
            p0.write(0, b"PRE_COMMIT_BASE_")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            u1 = lm.append_update(t1.transaction_id, 0, 0, b"PRE_COMMIT_BASE_", b"COMMITTED_DATA__")
            bm.fetch_page(0).write(0, b"COMMITTED_DATA__")
            bm.set_page_lsn(0, u1)
            bm.unpin_page(0, is_dirty=True)

            tm.commit_transaction(t1.transaction_id)
            lm.flush()

            # Abrupt crash simulation: buffer pool closed without disk flush
            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        # Restart and recover
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.winners == [1]
            assert result.losers == []
            assert result.records_redone == 1
            assert result.clrs_generated == 0
            assert result.updates_undone == 0

            # Verify page contents in buffer pool
            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"COMMITTED_DATA__"
            bm.unpin_page(0, is_dirty=False)

        # Verify page contents directly on persistent disk
        with DiskManager(db_path=temp_db_path) as dm:
            p0 = dm.read_page(0)
            assert p0.read(0, 16) == b"COMMITTED_DATA__"

    # -------------------------------------------------------------------------
    # Scenario 3: Incomplete transaction whose updates must be undone
    # -------------------------------------------------------------------------
    def test_scenario_03_incomplete_transaction_undone(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 3: Active uncommitted transaction is recognized as loser and rolled back to before-image."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            p0 = bm.allocate_page()
            p0.write(0, b"ORIGINAL_PAGE0__")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"ORIGINAL_PAGE0__", b"CRASHED_DIRTY___")
            lm.flush()

            # Abrupt crash while T1 is RUNNING
            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.winners == []
            assert result.losers == [1]
            assert result.clrs_generated == 1
            assert result.updates_undone == 1
            assert result.completed_transactions == [1]

            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"ORIGINAL_PAGE0__"
            bm.unpin_page(0, is_dirty=False)

        with DiskManager(db_path=temp_db_path) as dm:
            p0 = dm.read_page(0)
            assert p0.read(0, 16) == b"ORIGINAL_PAGE0__"

    # -------------------------------------------------------------------------
    # Scenario 4: Multiple interleaved transactions with mixed outcomes
    # -------------------------------------------------------------------------
    def test_scenario_04_multiple_interleaved_transactions_mixed_outcomes(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 4: Interleaved concurrent transactions with commit and abort outcomes roll back correctly."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            for _ in range(3):
                bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"BASE0_")
            bm.unpin_page(0, is_dirty=True)
            p1 = bm.fetch_page(1)
            p1.write(0, b"BASE1_")
            bm.unpin_page(1, is_dirty=True)
            p2 = bm.fetch_page(2)
            p2.write(0, b"BASE2_")
            bm.unpin_page(2, is_dirty=True)
            bm.flush_all_pages()

            # T1 updates P0, commits (winner)
            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"BASE0_", b"T1_MOD")
            tm.commit_transaction(t1.transaction_id)

            # T2 updates P1, remains uncommitted (loser)
            t2 = tm.begin_transaction()
            lm.append_update(t2.transaction_id, 1, 0, b"BASE1_", b"T2_MOD")

            # T3 updates P2, commits (winner)
            t3 = tm.begin_transaction()
            lm.append_update(t3.transaction_id, 2, 0, b"BASE2_", b"T3_MOD")
            tm.commit_transaction(t3.transaction_id)

            # T4 updates P1, remains uncommitted (loser)
            t4 = tm.begin_transaction()
            lm.append_update(t4.transaction_id, 1, 0, b"T2_MOD", b"T4_MOD")

            lm.flush()
            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.winners == [1, 3]
            assert result.losers == [2, 4]

            # P0: T1 committed modification preserved
            p0 = bm.fetch_page(0)
            assert p0.read(0, 6) == b"T1_MOD"
            bm.unpin_page(0, is_dirty=False)

            # P1: T4 and T2 rolled back in reverse order, restoring BASE1_
            p1 = bm.fetch_page(1)
            assert p1.read(0, 6) == b"BASE1_"
            bm.unpin_page(1, is_dirty=False)

            # P2: T3 committed modification preserved
            p2 = bm.fetch_page(2)
            assert p2.read(0, 6) == b"T3_MOD"
            bm.unpin_page(2, is_dirty=False)

        # Directly verify persistent disk
        with DiskManager(db_path=temp_db_path) as dm:
            assert dm.read_page(0).read(0, 6) == b"T1_MOD"
            assert dm.read_page(1).read(0, 6) == b"BASE1_"
            assert dm.read_page(2).read(0, 6) == b"T3_MOD"

    # -------------------------------------------------------------------------
    # Scenario 5: Multiple updates by the same transaction to the same page
    # -------------------------------------------------------------------------
    def test_scenario_05_multiple_updates_same_transaction_same_page(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 5: Multiple sequential updates to one page are undone in reverse order with valid CLR chains."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            p0 = bm.allocate_page()
            p0.write(0, b"AAAA_BBBB_CCCC")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            u1 = lm.append_update(t1.transaction_id, 0, 0, b"AAAA", b"1111")
            u2 = lm.append_update(t1.transaction_id, 0, 5, b"BBBB", b"2222")
            u3 = lm.append_update(t1.transaction_id, 0, 10, b"CCCC", b"3333")
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.losers == [1]
            assert result.clrs_generated == 3
            assert result.updates_undone == 3

            # Verify full page content restored in buffer
            p0 = bm.fetch_page(0)
            assert p0.read(0, 14) == b"AAAA_BBBB_CCCC"
            bm.unpin_page(0, is_dirty=False)

            # Inspect appended CLRs in WAL
            records = lm.read_all()
            clrs = [r for r in records if r.record_type == LogRecordType.CLR]
            assert len(clrs) == 3
            # Reverse order undo: first CLR undoes u3 (undo_next points to u2)
            assert clrs[0].undo_next_lsn == u2
            # second CLR undoes u2 (undo_next points to u1)
            assert clrs[1].undo_next_lsn == u1
            # third CLR undoes u1 (undo_next points to BEGIN)
            assert clrs[2].undo_next_lsn == 1

        with DiskManager(db_path=temp_db_path) as dm:
            assert dm.read_page(0).read(0, 14) == b"AAAA_BBBB_CCCC"

    # -------------------------------------------------------------------------
    # Scenario 6: Multiple transactions updating different pages
    # -------------------------------------------------------------------------
    def test_scenario_06_multiple_transactions_different_pages(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 6: Transactions operating on disjoint pages preserve page isolation during recovery."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=10, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            for i in range(4):
                p = bm.allocate_page()
                p.write(0, f"INIT_PAGE_{i}_DATA".encode("utf-8"))
                bm.unpin_page(i, is_dirty=True)
            bm.flush_all_pages()

            t0 = tm.begin_transaction()
            lm.append_update(t0.transaction_id, 0, 0, b"INIT_PAGE_0_DATA", b"COMMITTED_PAGE_0")
            tm.commit_transaction(t0.transaction_id)

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 1, 0, b"INIT_PAGE_1_DATA", b"UNCOMMITTED_PG1_")

            t2 = tm.begin_transaction()
            lm.append_update(t2.transaction_id, 2, 0, b"INIT_PAGE_2_DATA", b"COMMITTED_PAGE_2")
            tm.commit_transaction(t2.transaction_id)

            t3 = tm.begin_transaction()
            lm.append_update(t3.transaction_id, 3, 0, b"INIT_PAGE_3_DATA", b"UNCOMMITTED_PG3_")

            lm.flush()
            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=10, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.winners == [1, 3]
            assert result.losers == [2, 4]

            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"COMMITTED_PAGE_0"
            bm.unpin_page(0, is_dirty=False)

            p1 = bm.fetch_page(1)
            assert p1.read(0, 16) == b"INIT_PAGE_1_DATA"
            bm.unpin_page(1, is_dirty=False)

            p2 = bm.fetch_page(2)
            assert p2.read(0, 16) == b"COMMITTED_PAGE_2"
            bm.unpin_page(2, is_dirty=False)

            p3 = bm.fetch_page(3)
            assert p3.read(0, 16) == b"INIT_PAGE_3_DATA"
            bm.unpin_page(3, is_dirty=False)

        with DiskManager(db_path=temp_db_path) as dm:
            assert dm.read_page(0).read(0, 16) == b"COMMITTED_PAGE_0"
            assert dm.read_page(1).read(0, 16) == b"INIT_PAGE_1_DATA"
            assert dm.read_page(2).read(0, 16) == b"COMMITTED_PAGE_2"
            assert dm.read_page(3).read(0, 16) == b"INIT_PAGE_3_DATA"

    # -------------------------------------------------------------------------
    # Scenario 7: A transaction updating multiple pages before a crash
    # -------------------------------------------------------------------------
    def test_scenario_07_transaction_updating_multiple_pages(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 7: Single transaction modifying multiple pages is completely undone across all pages."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            for i in range(3):
                p = bm.allocate_page()
                p.write(0, f"CLEAN_P{i}___".encode("utf-8"))
                bm.unpin_page(i, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"CLEAN_P0___", b"DIRTY_P0___")
            lm.append_update(t1.transaction_id, 1, 0, b"CLEAN_P1___", b"DIRTY_P1___")
            lm.append_update(t1.transaction_id, 2, 0, b"CLEAN_P2___", b"DIRTY_P2___")
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.losers == [1]
            assert result.clrs_generated == 3
            assert set(result.pages_modified) == {0, 1, 2}

            for i in range(3):
                p = bm.fetch_page(i)
                assert p.read(0, 11) == f"CLEAN_P{i}___".encode("utf-8")
                bm.unpin_page(i, is_dirty=False)

        with DiskManager(db_path=temp_db_path) as dm:
            for i in range(3):
                assert dm.read_page(i).read(0, 11) == f"CLEAN_P{i}___".encode("utf-8")

    # -------------------------------------------------------------------------
    # Scenario 8: Recovery with a checkpoint present
    # -------------------------------------------------------------------------
    def test_scenario_08_recovery_with_valid_checkpoint(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 8: Recovery initializes state from fuzzy checkpoint snapshot and continues forward."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            cm = CheckpointManager(lm, tm, bm)

            p0 = bm.allocate_page()
            p0.write(0, b"BASE_CP_VAL_")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            # Pre-checkpoint transaction commits
            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"BASE_CP_VAL_", b"T1_PRE_CP_")
            tm.commit_transaction(t1.transaction_id)

            # Pre-checkpoint transaction continues running
            t2 = tm.begin_transaction()
            lm.append_update(t2.transaction_id, 0, 0, b"T1_PRE_CP_", b"T2_PRE_CP_")
            lm.flush()

            # Create durable checkpoint
            cp_info = cm.create_checkpoint()
            assert cp_info is not None

            # Post-checkpoint: T2 commits
            tm.commit_transaction(t2.transaction_id)

            # Post-checkpoint: T3 begins, updates, crashes
            t3 = tm.begin_transaction()
            lm.append_update(t3.transaction_id, 0, 0, b"T2_PRE_CP_", b"T3_POST_CP")
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            cm = CheckpointManager(lm, TransactionManager(lm), bm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm, checkpoint_manager=cm)
            result = rm.recover()

            assert result.is_success is True
            assert result.analysis_result.analysis_start_lsn == cp_info.begin_lsn
            assert result.winners == [1, 2]
            assert result.losers == [3]

            p0 = bm.fetch_page(0)
            assert p0.read(0, 10) == b"T2_PRE_CP_"
            bm.unpin_page(0, is_dirty=False)

        with DiskManager(db_path=temp_db_path) as dm:
            assert dm.read_page(0).read(0, 10) == b"T2_PRE_CP_"

    # -------------------------------------------------------------------------
    # Scenario 9: Recovery when checkpoint is absent or incomplete
    # -------------------------------------------------------------------------
    def test_scenario_09_recovery_checkpoint_absent_or_incomplete(
        self, tmp_path: Path
    ) -> None:
        """Scenario 9: Recovery with checkpoint absent, and with incomplete checkpoint (crash at CHECKPOINT_BEGIN)."""
        # Part A: Checkpoint completely absent
        db_absent = tmp_path / "absent_cp.db"
        wal_absent = tmp_path / "absent_cp.wal"
        with DiskManager(db_path=db_absent) as dm, LogManager(wal_path=wal_absent) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            p = bm.allocate_page()
            p.write(0, b"ABSENT_CP_INIT__")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"ABSENT_CP_INIT__", b"ABSENT_CP_MUT1__")
            tm.commit_transaction(t1.transaction_id)

            t2 = tm.begin_transaction()
            lm.append_update(t2.transaction_id, 0, 0, b"ABSENT_CP_MUT1__", b"ABSENT_CP_MUT2__")
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=db_absent) as dm, LogManager(wal_path=wal_absent) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm, checkpoint_manager=None)
            res_absent = rm.recover()

            assert res_absent.is_success is True
            assert res_absent.analysis_result.checkpoint_info is None
            assert res_absent.winners == [1]
            assert res_absent.losers == [2]

            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"ABSENT_CP_MUT1__"
            bm.unpin_page(0, is_dirty=False)

        with DiskManager(db_path=db_absent) as dm:
            assert dm.read_page(0).read(0, 16) == b"ABSENT_CP_MUT1__"

        # Part B: Incomplete checkpoint via subprocess crash at CHECKPOINT_BEGIN
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.transaction.transaction_manager import TransactionManager
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.fault.constants import FaultPoint, DEFAULT_CRASH_EXIT_CODE
from app.fault.fault_injector import fault_injector

db_path = Path("inc_cp.db")
wal_path = Path("inc_cp.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    cm = CheckpointManager(lm, tm, bm)

    bm.allocate_page()
    p = bm.fetch_page(0)
    p.write(0, b"CLEAN_DATA_P0___")
    bm.unpin_page(0, is_dirty=True)
    bm.flush_all_pages()

    t1 = tm.begin_transaction()
    lm.append_update(t1.transaction_id, 0, 0, b"CLEAN_DATA_P0___", b"MODIFIED_BEFORE_")
    lm.flush()

    # Enable fault point at CHECKPOINT_BEGIN to crash before CHECKPOINT_END
    fault_injector.enable(FaultPoint.CHECKPOINT_BEGIN, exit_code={DEFAULT_CRASH_EXIT_CODE})
    cm.create_checkpoint()
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        db_file = tmp_path / "inc_cp.db"
        wal_file = tmp_path / "inc_cp.wal"

        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, 5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            # Incomplete checkpoint ignored -> analysis started from first log record
            assert result.analysis_result.checkpoint_info is None
            assert result.losers == [1]
            assert result.clrs_generated == 1

            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"CLEAN_DATA_P0___"
            bm.unpin_page(0, is_dirty=False)

        with DiskManager(db_file) as dm:
            assert dm.read_page(0).read(0, 16) == b"CLEAN_DATA_P0___"

    # -------------------------------------------------------------------------
    # Scenario 10: Crash during Redo followed by another recovery attempt
    # -------------------------------------------------------------------------
    def test_scenario_10_crash_during_redo_subprocess_restart(
        self, tmp_path: Path
    ) -> None:
        """Scenario 10: Subprocess terminates at AFTER_REDO_BEFORE_UNDO; restart recovery completes safely."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.transaction.transaction_manager import TransactionManager
from app.recovery.recovery_manager import RecoveryManager
from app.fault.constants import FaultPoint, DEFAULT_CRASH_EXIT_CODE
from app.fault.fault_injector import fault_injector

db_path = Path("crash_redo.db")
wal_path = Path("crash_redo.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    bm.allocate_page()
    p = bm.fetch_page(0)
    p.write(0, b"PRE_REDO_VAL____")
    bm.unpin_page(0, is_dirty=True)
    bm.flush_all_pages()

    t1 = tm.begin_transaction()
    lm.append_update(t1.transaction_id, 0, 0, b"PRE_REDO_VAL____", b"CRASH_MUTATION__")
    lm.flush()

fault_injector.enable(FaultPoint.AFTER_REDO_BEFORE_UNDO, exit_code={DEFAULT_CRASH_EXIT_CODE})

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
    rm.recover()
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        db_file = tmp_path / "crash_redo.db"
        wal_file = tmp_path / "crash_redo.wal"

        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, 5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.losers == [1]
            assert result.clrs_generated == 1

            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"PRE_REDO_VAL____"
            bm.unpin_page(0, is_dirty=False)

        with DiskManager(db_file) as dm:
            assert dm.read_page(0).read(0, 16) == b"PRE_REDO_VAL____"

    # -------------------------------------------------------------------------
    # Scenario 11: Crash during Undo followed by another recovery attempt
    # -------------------------------------------------------------------------
    def test_scenario_11_crash_during_undo_subprocess_restart(
        self, tmp_path: Path
    ) -> None:
        """Scenario 11: Subprocess terminates mid-Undo; subsequent recovery resumes from durable CLR."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.transaction.transaction_manager import TransactionManager
from app.recovery.recovery_manager import RecoveryManager
from app.fault.constants import FaultPoint, DEFAULT_CRASH_EXIT_CODE
from app.fault.fault_injector import fault_injector

db_path = Path("crash_undo.db")
wal_path = Path("crash_undo.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    bm.allocate_page()
    p = bm.fetch_page(0)
    p.write(0, b"AAAA_BBBB")
    bm.unpin_page(0, is_dirty=True)
    bm.flush_all_pages()

    t1 = tm.begin_transaction()
    lm.append_update(t1.transaction_id, 0, 0, b"AAAA", b"1111")
    lm.append_update(t1.transaction_id, 0, 5, b"BBBB", b"2222")
    lm.flush()

# Crash after the first update is undone and page is flushed
fault_injector.enable(FaultPoint.DURING_UNDO_AFTER_PAGE_FLUSH, exit_code={DEFAULT_CRASH_EXIT_CODE})

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
    rm.recover()
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        db_file = tmp_path / "crash_undo.db"
        wal_file = tmp_path / "crash_undo.wal"

        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, 5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            # Only remaining update was undone on restart
            assert result.undo_result.clrs_generated == 1
            assert result.completed_transactions == [1]

            p0 = bm.fetch_page(0)
            assert p0.read(0, 9) == b"AAAA_BBBB"
            bm.unpin_page(0, is_dirty=False)

        with DiskManager(db_file) as dm:
            assert dm.read_page(0).read(0, 9) == b"AAAA_BBBB"

    # -------------------------------------------------------------------------
    # Scenario 12: Crash after CLR flush before page update flush
    # -------------------------------------------------------------------------
    def test_scenario_12_crash_after_clr_flush_before_page_flush_subprocess(
        self, tmp_path: Path
    ) -> None:
        """Scenario 12: Crash after CLR flush; Redo replays CLR and Undo jumps past it to avoid duplicate undo."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.transaction.transaction_manager import TransactionManager
from app.recovery.recovery_manager import RecoveryManager
from app.fault.constants import FaultPoint, DEFAULT_CRASH_EXIT_CODE
from app.fault.fault_injector import fault_injector

db_path = Path("clr_flush_crash.db")
wal_path = Path("clr_flush_crash.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    bm.allocate_page()
    p = bm.fetch_page(0)
    p.write(0, b"ORIGINAL_PAGE0__")
    bm.unpin_page(0, is_dirty=True)
    bm.flush_all_pages()

    t1 = tm.begin_transaction()
    lm.append_update(t1.transaction_id, 0, 0, b"ORIGINAL_PAGE0__", b"MUTATED_PAGE0___")
    lm.flush()

# Crash after CLR is flushed to WAL but before page is flushed to disk
fault_injector.enable(
    FaultPoint.DURING_UNDO_AFTER_CLR_FLUSH_BEFORE_PAGE_FLUSH,
    exit_code={DEFAULT_CRASH_EXIT_CODE}
)

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
    rm.recover()
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        db_file = tmp_path / "clr_flush_crash.db"
        wal_file = tmp_path / "clr_flush_crash.wal"

        # Explicitly verify that CLR was durable in WAL before restart recovery
        with LogManager(wal_file) as lm:
            clrs = [r for r in lm.read_all() if r.record_type == LogRecordType.CLR]
            assert len(clrs) == 1
            assert clrs[0].compensation_image == b"ORIGINAL_PAGE0__"
            assert lm.flushed_lsn >= clrs[0].lsn

        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, 5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            # Redo reconstructed the CLR compensation
            assert result.redo_result.records_redone == 2  # Update + CLR
            # Undo recognized CLR, generating 0 additional CLRs
            assert result.undo_result.clrs_generated == 0

            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"ORIGINAL_PAGE0__"
            bm.unpin_page(0, is_dirty=False)

        with DiskManager(db_file) as dm:
            assert dm.read_page(0).read(0, 16) == b"ORIGINAL_PAGE0__"

    # -------------------------------------------------------------------------
    # Scenario 13: Crash after Undo completed before completion recorded
    # -------------------------------------------------------------------------
    def test_scenario_13_crash_after_undo_before_complete_subprocess(
        self, tmp_path: Path
    ) -> None:
        """Scenario 13: Crash at AFTER_UNDO_BEFORE_COMPLETE; restart recovery completes with 0 additional undo steps."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.transaction.transaction_manager import TransactionManager
from app.recovery.recovery_manager import RecoveryManager
from app.fault.constants import FaultPoint, DEFAULT_CRASH_EXIT_CODE
from app.fault.fault_injector import fault_injector

db_path = Path("undo_done_crash.db")
wal_path = Path("undo_done_crash.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    bm.allocate_page()
    p = bm.fetch_page(0)
    p.write(0, b"RECOVERED_INIT__")
    bm.unpin_page(0, is_dirty=True)
    bm.flush_all_pages()

    t1 = tm.begin_transaction()
    lm.append_update(t1.transaction_id, 0, 0, b"RECOVERED_INIT__", b"TMP_MUTATION1___")
    lm.flush()

fault_injector.enable(FaultPoint.AFTER_UNDO_BEFORE_COMPLETE, exit_code={DEFAULT_CRASH_EXIT_CODE})

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
    rm.recover()
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        db_file = tmp_path / "undo_done_crash.db"
        wal_file = tmp_path / "undo_done_crash.wal"

        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, 5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.losers == []
            assert result.clrs_generated == 0

            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"RECOVERED_INIT__"
            bm.unpin_page(0, is_dirty=False)

        with DiskManager(db_file) as dm:
            assert dm.read_page(0).read(0, 16) == b"RECOVERED_INIT__"

    # -------------------------------------------------------------------------
    # Scenario 14: Repeated recovery on an already recovered database
    # -------------------------------------------------------------------------
    def test_scenario_14_repeated_recovery_idempotence(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 14: Executing recovery repeatedly on the same state produces identical idempotent results."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            p0 = bm.allocate_page()
            p0.write(0, b"IDEMPOTENT_BASE_")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"IDEMPOTENT_BASE_", b"MUTATED_VAL____")
            lm.flush()

            # First recovery run
            rm1 = RecoveryManager(log_manager=lm, buffer_manager=bm)
            res1 = rm1.recover()
            assert res1.is_success is True
            assert res1.clrs_generated == 1
            log_count_after_run1 = len(lm.read_all())
            page_lsn_after_run1 = bm.get_page_lsn(0)

            # Second recovery run
            rm2 = RecoveryManager(log_manager=lm, buffer_manager=bm)
            res2 = rm2.recover()
            assert res2.is_success is True
            assert res2.losers == []
            assert res2.clrs_generated == 0
            assert res2.updates_undone == 0
            assert res2.pages_modified == []
            # Proves zero additional log records generated and page LSN unchanged
            assert len(lm.read_all()) == log_count_after_run1
            assert bm.get_page_lsn(0) == page_lsn_after_run1

            # Third recovery run
            rm3 = RecoveryManager(log_manager=lm, buffer_manager=bm)
            res3 = rm3.recover()
            assert res3.is_success is True
            assert res3.clrs_generated == 0
            assert len(lm.read_all()) == log_count_after_run1
            assert bm.get_page_lsn(0) == page_lsn_after_run1

            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"IDEMPOTENT_BASE_"
            bm.unpin_page(0, is_dirty=False)

        with DiskManager(db_path=temp_db_path) as dm:
            assert dm.read_page(0).read(0, 16) == b"IDEMPOTENT_BASE_"

    # -------------------------------------------------------------------------
    # Scenario 15: Corrupted or invalid WAL records and invalid LSN chains
    # -------------------------------------------------------------------------
    def test_scenario_15_corrupted_wal_and_invalid_lsn_chains(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 15: WAL corruption, cyclic chains, CRC mismatch, and truncation fail safely without false success."""
        # 1. Corrupted file magic
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            lm.append_begin(1)
            lm.flush()

        with open(temp_wal_path, "r+b") as f:
            f.seek(0)
            f.write(b"CORRUPT_BYTES__")

        with pytest.raises(LogCorruptionError):
            with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
                pass

        # 2. Cyclic log chain in Undo
        wal_cyclic = temp_wal_path.with_name("cyclic.wal")
        db_cyclic = temp_db_path.with_name("cyclic.db")
        with DiskManager(db_path=db_cyclic) as dm, LogManager(wal_path=wal_cyclic) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            bm.flush_all_pages()

            lm.append_begin(1)
            # Create update with prev_lsn pointing to itself (cyclic)
            rec = LogRecord(
                lsn=2,
                record_type=LogRecordType.UPDATE,
                transaction_id=1,
                prev_lsn=2,  # Points to itself
                page_id=0,
                offset=0,
                before_image=b"A",
                after_image=b"B",
            )
            lm.append_log(rec)
            lm.flush()

            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            with pytest.raises(UndoError, match=r"Detected (cyclic|invalid forward-pointing) log chain"):
                rm.recover(raise_on_error=True)

            res = rm.recover(raise_on_error=False)
            assert res.is_success is False
            assert res.stage == "FAILED_AT_UNDO"

        # 3. CRC mismatch in log record
        wal_crc = temp_wal_path.with_name("crc.wal")
        db_crc = temp_db_path.with_name("crc.db")
        with DiskManager(db_path=db_crc) as dm, LogManager(wal_path=wal_crc) as lm:
            lm.append_begin(1)
            lm.append_update(1, 0, 0, b"ORIGINAL_CRC", b"NEW_DATA_CRC")
            lm.flush()

        # Corrupt bytes in the record payload on disk
        with open(wal_crc, "r+b") as f:
            f.seek(32)
            f.write(b"\xFF\xFF\xFF\xFF")

        with pytest.raises(LogCorruptionError):
            with LogManager(wal_path=wal_crc) as lm:
                pass

        # 4. Truncated record at end of WAL
        wal_trunc = temp_wal_path.with_name("trunc.wal")
        db_trunc = temp_db_path.with_name("trunc.db")
        with DiskManager(db_path=db_trunc) as dm, LogManager(wal_path=wal_trunc) as lm:
            lm.append_begin(1)
            lm.append_update(1, 0, 0, b"TRUNC_OLD", b"TRUNC_NEW")
            lm.flush()

        size = wal_trunc.stat().st_size
        with open(wal_trunc, "r+b") as f:
            f.truncate(size - 8)

        with pytest.raises(LogCorruptionError):
            with LogManager(wal_path=wal_trunc) as lm:
                pass

    # -------------------------------------------------------------------------
    # Scenario 16: Invalid transaction or page references
    # -------------------------------------------------------------------------
    def test_scenario_16_invalid_transaction_or_page_references(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 16: Out-of-bounds page offset/length or non-existent page IDs fail safely without corrupting memory."""
        # 1. Out-of-bounds update beyond PAGE_SIZE
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            bm.flush_all_pages()

            lm.append_begin(1)
            rec = object.__new__(LogRecord)
            rec._lsn = 2
            rec._record_type = LogRecordType.UPDATE
            rec._transaction_id = 1
            rec._prev_lsn = 1
            rec._page_id = 0
            rec._offset = PAGE_SIZE - 2
            rec._before_image = b"OVERFLOW_PAYLOAD"  # Exceeds PAGE_SIZE
            rec._after_image = b"OVERFLOW_PAYLOAD"
            rec._checkpoint_payload = None
            rec._undo_next_lsn = None
            lm.append_log(rec)
            lm.flush()

            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            with pytest.raises(RedoError, match="Invalid UPDATE bounds"):
                rm.recover(raise_on_error=True)

            res = rm.recover(raise_on_error=False)
            assert res.is_success is False
            assert res.stage == "FAILED_AT_REDO"

        # 2. Non-existent page ID on disk
        wal_p99 = temp_wal_path.with_name("p99.wal")
        db_p99 = temp_db_path.with_name("p99.db")
        with DiskManager(db_path=db_p99) as dm, LogManager(wal_path=wal_p99) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            lm.append_begin(1)
            rec = LogRecord(
                lsn=2,
                record_type=LogRecordType.UPDATE,
                transaction_id=1,
                prev_lsn=1,
                page_id=9999,  # Non-existent page
                offset=0,
                before_image=b"X",
                after_image=b"Y",
            )
            lm.append_log(rec)
            lm.flush()

            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            with pytest.raises(PageNotFoundError):
                rm.recover(raise_on_error=True)

            res = rm.recover(raise_on_error=False)
            assert res.is_success is False
            assert res.stage == "FAILED_AT_REDO"

    # -------------------------------------------------------------------------
    # Scenario 17: Recovery failures at each orchestration stage
    # -------------------------------------------------------------------------
    def test_scenario_17_recovery_failures_at_each_stage(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 17: Injected stage errors record precise stage status and avoid false success."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            bm.flush_all_pages()
            lm.append_begin(1)
            lm.append_update(1, 0, 0, b"A", b"B")
            lm.flush()

            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)

            # Stage 1: Analysis Failure
            orig_analysis = Analysis.execute
            Analysis.execute = lambda s: (_ for _ in ()).throw(AnalysisError("Analysis failure"))
            try:
                res = rm.recover(raise_on_error=False)
                assert res.is_success is False
                assert res.stage == "FAILED_AT_ANALYSIS"
                assert "Analysis failure" in str(res.error_message)
            finally:
                Analysis.execute = orig_analysis

            # Stage 2: Redo Failure
            orig_redo = Redo.execute
            Redo.execute = lambda s, a: (_ for _ in ()).throw(RedoError("Redo failure"))
            try:
                res = rm.recover(raise_on_error=False)
                assert res.is_success is False
                assert res.stage == "FAILED_AT_REDO"
                assert "Redo failure" in str(res.error_message)
            finally:
                Redo.execute = orig_redo

            # Stage 3: Undo Failure
            orig_undo = Undo.execute
            Undo.execute = lambda s, a: (_ for _ in ()).throw(UndoError("Undo failure"))
            try:
                res = rm.recover(raise_on_error=False)
                assert res.is_success is False
                assert res.stage == "FAILED_AT_UNDO"
                assert "Undo failure" in str(res.error_message)
            finally:
                Undo.execute = orig_undo

            # Stage 4: Verification Failure (incomplete undo outcome)
            orig_undo_exec = Undo.execute

            def incomplete_undo(s, a):
                return UndoResult(
                    loser_transactions=1,
                    transactions_undone=0,
                    clrs_generated=0,
                    updates_undone=0,
                    pages_modified=[],
                    records_skipped=0,
                    last_processed_lsn=None,
                    completed_transactions=[],
                    remaining_losers=[1],
                    is_complete=False,
                )

            Undo.execute = incomplete_undo
            try:
                res = rm.recover(raise_on_error=False)
                assert res.is_success is False
                assert res.stage == "FAILED_AT_VERIFICATION"
                assert "Undo phase ended with remaining loser transactions" in str(res.error_message)
            finally:
                Undo.execute = orig_undo_exec

    # -------------------------------------------------------------------------
    # Scenario 18: File-based recovery across process restarts
    # -------------------------------------------------------------------------
    def test_scenario_18_file_based_recovery_across_restarts(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 18: Persistent files recover cleanly using recover_from_files convenience method."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            p0 = bm.allocate_page()
            p0.write(0, b"FILE_BASED_TEST_")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"FILE_BASED_TEST_", b"FILE_MUTATED____")
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        # Execute recovery directly via files
        result = RecoveryManager.recover_from_files(
            db_path=temp_db_path,
            wal_path=temp_wal_path,
            pool_size=5,
        )

        assert result.is_success is True
        assert result.losers == [1]
        assert result.clrs_generated == 1

        # Reopen files and verify restored disk state
        with DiskManager(db_path=temp_db_path) as dm:
            p0 = dm.read_page(0)
            assert p0.read(0, 16) == b"FILE_BASED_TEST_"

    # -------------------------------------------------------------------------
    # Scenario 19: Buffer-pool pin/unpin safety and page consistency
    # -------------------------------------------------------------------------
    def test_scenario_19_buffer_pool_pin_safety_and_page_consistency(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 19: Buffer pool pin counts are strictly 0 and all pages clean after successful and failed recovery."""
        # 1. Successful recovery pin release
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            p0 = bm.allocate_page()
            p0.write(0, b"PIN_SAFETY_TEST_")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            # Baseline check before recovery: pin count is 0
            assert bm.get_pin_count(0) == 0

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"PIN_SAFETY_TEST_", b"DIRTY_PIN_MUT___")
            lm.flush()

            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True

            # Check all resident frames: pin count must be exactly 0, is_dirty must be False
            for page_id in bm.get_resident_pages():
                assert bm.get_pin_count(page_id) == 0
                assert bm.is_dirty(page_id) is False

        # 2. Failing recovery path: pins must also be released
        wal_fail = temp_wal_path.with_name("pin_fail.wal")
        db_fail = temp_db_path.with_name("pin_fail.db")
        with DiskManager(db_path=db_fail) as dm, LogManager(wal_path=wal_fail) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            p = bm.allocate_page()
            bm.unpin_page(0, is_dirty=False)
            assert bm.get_pin_count(0) == 0

            lm.append_begin(1)
            rec = object.__new__(LogRecord)
            rec._lsn = 2
            rec._record_type = LogRecordType.UPDATE
            rec._transaction_id = 1
            rec._prev_lsn = 1
            rec._page_id = 0
            rec._offset = PAGE_SIZE - 2
            rec._before_image = b"OVERFLOW"
            rec._after_image = b"OVERFLOW"
            rec._checkpoint_payload = None
            rec._undo_next_lsn = None
            lm.append_log(rec)
            lm.flush()

            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            res = rm.recover(raise_on_error=False)
            assert res.is_success is False

            # Verify no leaked pins even after failure
            for page_id in bm.get_resident_pages():
                assert bm.get_pin_count(page_id) == 0

    # -------------------------------------------------------------------------
    # Scenario 20: Compatibility with all existing public APIs and tests
    # -------------------------------------------------------------------------
    def test_scenario_20_public_apis_and_backward_compatibility(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 20: Standalone Analysis, Redo, Undo, and RecoveryManager class methods remain fully compatible."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            bm.flush_all_pages()

            lm.append_begin(1)
            lm.append_update(1, 0, 0, b"API_TEST_OLD", b"API_TEST_NEW")
            lm.flush()

            # Standalone Analysis classmethod
            a_res = Analysis.analyze(lm)
            assert a_res.losers == [1]

            # Standalone Redo classmethods
            r_res1 = Redo.recover(lm, bm, a_res)
            assert r_res1.records_redone == 1
            r_res2 = Redo.redo(lm, bm, a_res)
            assert r_res2.records_redone == 0  # Idempotently skipped

            # Standalone Undo classmethod
            u_res = Undo.recover(lm, bm, a_res)
            assert u_res.clrs_generated == 1
            assert u_res.is_complete is True

            # RecoveryManager classmethod and aliases
            rm = RecoveryManager(lm, bm)
            assert "RecoveryManager" in repr(rm)
            assert rm.is_recovered is False

            # Verify RecoveryResult properties and immutability
            res = rm.run()
            assert res.is_success is True
            assert rm.is_recovered is True
            assert isinstance(res.to_dict(), dict)
            assert "RecoveryResult" in repr(res)

            # Modifying copy does not mutate result
            losers = res.losers
            losers.append(999)
            assert res.losers == []
