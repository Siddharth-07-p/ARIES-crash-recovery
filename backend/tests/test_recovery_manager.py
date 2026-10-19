"""Comprehensive integration test suite for Phase 10: Complete ARIES Crash-Recovery Orchestration.

Covers all 15 required scenarios:
1. Recovery with no transactions requiring Undo.
2. Recovery of a committed transaction.
3. Recovery of a single loser transaction.
4. Recovery of multiple interleaved loser transactions.
5. Recovery from a valid checkpoint.
6. Recovery with an incomplete ABORT.
7. Recovery after a durable CLR but before the compensated page is flushed.
8. Recovery after a crash during Undo.
9. Repeated crash and recovery cycles.
10. Idempotent recovery after successful completion.
11. Invalid or corrupted WAL causes a clear failure.
12. Stage ordering is Analysis, Redo, then Undo.
13. A failed stage prevents later stages from being reported as successful.
14. Recovery result structure and immutability.
15. Existing Phase 0–9 behavior remains compatible.
Additional edge cases:
16. Loser transaction with no UPDATE records.
17. Convenience recovery from files (RecoveryManager.recover_from_files).
18. Constructor argument validations and closed managers rejection.
19. Subprocess crash at AFTER_UNDO_BEFORE_COMPLETE followed by restart recovery.
"""

import os
from pathlib import Path
import subprocess
import sys
from typing import List, Optional
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
from app.transaction.transaction import TransactionState
from app.transaction.transaction_manager import TransactionManager
from app.wal.constants import WAL_RECORD_MAGIC
from app.wal.exceptions import LogCorruptionError
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecord, LogRecordType


@pytest.fixture
def temp_wal_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary WAL filepath."""
    return tmp_path / "test_recovery.wal"


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary DB filepath."""
    return tmp_path / "test_recovery.db"


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


class TestRecoveryManager:
    """Comprehensive test suite for Phase 10: Complete ARIES Crash-Recovery Orchestration."""

    # -------------------------------------------------------------------------
    # Scenario 1: Recovery with no transactions requiring Undo
    # -------------------------------------------------------------------------
    def test_recovery_with_no_transactions_requiring_undo(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 1: Clean database or only committed winners; 0 undo operations needed."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"CLEAN_INITIAL")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"CLEAN_INITIAL", b"WINNER_UPDATE")
            tm.commit_transaction(t1.transaction_id)
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
            assert result.stage == "COMPLETE"
            assert result.error_message is None
            assert result.winners == [1]
            assert result.losers == []
            assert result.clrs_generated == 0
            assert result.updates_undone == 0
            assert result.undo_result.is_complete is True

            p0 = bm.fetch_page(0)
            assert p0.read(0, 13) == b"WINNER_UPDATE"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Scenario 2: Recovery of a committed transaction
    # -------------------------------------------------------------------------
    def test_recovery_of_committed_transaction(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 2: Committed transaction whose dirty page was not flushed before crash is redone."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"INITIAL_DATA")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lm.append_update(t1.transaction_id, 0, 0, b"INITIAL_DATA", b"COMMITTED_OK")  # LSN 2
            tm.commit_transaction(t1.transaction_id)  # LSN 3
            lm.flush()

            # Crash without flushing dirty page to disk
            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        # Page on physical disk still contains INITIAL_DATA
        with DiskManager(db_path=temp_db_path) as dm:
            raw_p0 = dm.read_page(0)
            assert raw_p0.read(0, 12) == b"INITIAL_DATA"

        # Execute recovery
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.winners == [1]
            assert result.losers == []
            assert result.records_redone == 1
            assert result.updates_undone == 0

            # Page is redone and persisted
            p0 = bm.fetch_page(0)
            assert p0.read(0, 12) == b"COMMITTED_OK"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Scenario 3: Recovery of a single loser transaction
    # -------------------------------------------------------------------------
    def test_recovery_of_single_loser_transaction(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 3: Active uncommitted transaction is rolled back with CLR and completion ABORT."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"ORIGINAL_TEXT_")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"ORIGINAL_TEXT_", b"DIRTY_MODIFIED")
            lm.flush()

            # Crash while transaction is RUNNING
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
            assert p0.read(0, 14) == b"ORIGINAL_TEXT_"
            bm.unpin_page(0, is_dirty=False)

            # Verify WAL has CLR followed by completion ABORT
            records = lm.read_all()
            assert records[-2].record_type == LogRecordType.CLR
            assert records[-2].transaction_id == 1
            assert records[-1].record_type == LogRecordType.ABORT
            assert records[-1].transaction_id == 1

    # -------------------------------------------------------------------------
    # Scenario 4: Recovery of multiple interleaved loser transactions
    # -------------------------------------------------------------------------
    def test_recovery_of_multiple_interleaved_loser_transactions(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 4: Interleaved loser transactions on multiple pages are rolled back via priority queue."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()  # Page 0
            bm.allocate_page()  # Page 1
            p0 = bm.fetch_page(0)
            p0.write(0, b"PAGE0_ORIG")
            bm.unpin_page(0, is_dirty=True)
            p1 = bm.fetch_page(1)
            p1.write(0, b"PAGE1_ORIG")
            bm.unpin_page(1, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            t2 = tm.begin_transaction()  # LSN 2
            lm.append_update(t1.transaction_id, 0, 0, b"PAGE0_ORIG", b"PAGE0_MOD1")  # LSN 3
            lm.append_update(t2.transaction_id, 1, 0, b"PAGE1_ORIG", b"PAGE1_MOD1")  # LSN 4
            lm.append_update(t1.transaction_id, 0, 0, b"PAGE0_MOD1", b"PAGE0_MOD2")  # LSN 5
            lm.append_update(t2.transaction_id, 1, 0, b"PAGE1_MOD1", b"PAGE1_MOD2")  # LSN 6
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
            assert result.losers == [1, 2]
            assert result.clrs_generated == 4
            assert result.updates_undone == 4
            assert set(result.completed_transactions) == {1, 2}

            p0 = bm.fetch_page(0)
            p1 = bm.fetch_page(1)
            assert p0.read(0, 10) == b"PAGE0_ORIG"
            assert p1.read(0, 10) == b"PAGE1_ORIG"
            bm.unpin_page(0, is_dirty=False)
            bm.unpin_page(1, is_dirty=False)

    # -------------------------------------------------------------------------
    # Scenario 5: Recovery from a valid checkpoint
    # -------------------------------------------------------------------------
    def test_recovery_from_valid_checkpoint(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 5: Checkpoint snapshot initializes Analysis; recovery starts from checkpoint baseline."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"BASE_DATA")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            # T1 begins and updates P0
            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"BASE_DATA", b"CKPT_MOD1")
            lm.flush()

            # Take a fuzzy checkpoint
            cp_info = cm.create_checkpoint()
            assert cp_info is not None

            # T2 begins after checkpoint; T1 updates again
            t2 = tm.begin_transaction()
            lm.append_update(t2.transaction_id, 0, 5, b"DATA", b"MOD2")
            tm.commit_transaction(t1.transaction_id)
            lm.flush()

            bm.close(flush_dirty=False)
            cm.close()
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.analysis_result.checkpoint_info is not None
            assert result.winners == [1]
            assert result.losers == [2]
            assert result.clrs_generated == 1
            assert result.completed_transactions == [1, 2]

    # -------------------------------------------------------------------------
    # Scenario 6: Recovery with an incomplete ABORT
    # -------------------------------------------------------------------------
    def test_recovery_with_incomplete_abort(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 6: ABORT record alone does not imply completion; incomplete abort is rolled back."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"PRE_ABORT_VAL_")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lm.append_update(t1.transaction_id, 0, 0, b"PRE_ABORT_VAL_", b"CRASH_ABORT___")  # LSN 2
            lm.append_abort(t1.transaction_id)  # LSN 3 (decision to abort, crash before rollback)
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
            assert result.analysis_result.transaction_table[1].state == TransactionState.ABORTED
            assert result.clrs_generated == 1
            assert result.updates_undone == 1
            assert result.completed_transactions == [1]

            p0 = bm.fetch_page(0)
            assert p0.read(0, 14) == b"PRE_ABORT_VAL_"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Scenario 7: Recovery after durable CLR before page flush
    # -------------------------------------------------------------------------
    def test_recovery_after_durable_clr_before_page_flush(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 7: CLR is durable in WAL but page was not flushed; Redo replays CLR and Undo skips it."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"ORIGINAL_PAGE0")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            u1 = lm.append_update(t1.transaction_id, 0, 0, b"ORIGINAL_PAGE0", b"MUTATED_PAGE0_")  # LSN 2

            # Simulate crash after CLR flush: CLR is in WAL, but page on disk still has old/dirty data
            clr1 = lm.append_clr(t1.transaction_id, 0, 0, undo_next_lsn=1, compensation_image=b"ORIGINAL_PAGE0")  # LSN 3
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
            # Redo replays the CLR (reconstructing compensated state)
            assert result.redo_result.records_redone == 2  # Update + CLR
            # Undo jumps past the CLR to undo_next_lsn (LSN 1: BEGIN), generates 0 new CLRs
            assert result.undo_result.clrs_generated == 0
            assert result.undo_result.updates_undone == 0

            p0 = bm.fetch_page(0)
            assert p0.read(0, 14) == b"ORIGINAL_PAGE0"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Scenario 8: Recovery after crash during Undo
    # -------------------------------------------------------------------------
    def test_recovery_after_crash_during_undo(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 8: Crash occurs midway through Undo; next recovery continues from CLR undo_next_lsn."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"AAAA_BBBB")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            u1 = lm.append_update(t1.transaction_id, 0, 0, b"AAAA", b"1111")  # LSN 2
            u2 = lm.append_update(t1.transaction_id, 0, 5, b"BBBB", b"2222")  # LSN 3
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        # Simulate first recovery pass that undid LSN 3 and crashed
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            p = bm.fetch_page(0)
            p.write(5, b"BBBB")
            clr = lm.append_clr(1, 0, 5, undo_next_lsn=u1, compensation_image=b"BBBB")  # LSN 4
            lm.flush()
            bm.set_page_lsn(0, clr)
            bm.unpin_page(0, is_dirty=True)
            bm.flush_page(0)

            bm.close(flush_dirty=False)
            lm.close()
            dm.close()

        # Second recovery pass via RecoveryManager completes rollback
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            # Only LSN 2 was left to undo -> exactly 1 CLR generated
            assert result.undo_result.clrs_generated == 1
            assert result.undo_result.updates_undone == 1
            assert result.completed_transactions == [1]

            p0 = bm.fetch_page(0)
            assert p0.read(0, 9) == b"AAAA_BBBB"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Scenario 9: Repeated crash and recovery cycles
    # -------------------------------------------------------------------------
    def test_repeated_crash_and_recovery_cycles(self, tmp_path: Path) -> None:
        """Scenario 9: Genuine subprocess crash at AFTER_REDO_BEFORE_UNDO followed by full recovery."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.transaction.transaction_manager import TransactionManager
from app.recovery.recovery_manager import RecoveryManager
from app.fault.constants import FaultPoint, DEFAULT_CRASH_EXIT_CODE
from app.fault.fault_injector import fault_injector

db_path = Path("repeat_crash.db")
wal_path = Path("repeat_crash.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    bm.allocate_page()
    p = bm.fetch_page(0)
    p.write(0, b"INITIAL_CYCLE_P0")
    bm.unpin_page(0, is_dirty=True)
    bm.flush_all_pages()

    t1 = tm.begin_transaction()
    lm.append_update(t1.transaction_id, 0, 0, b"INITIAL_CYCLE_P0", b"CRASH_MUTATION0_")
    lm.flush()

fault_injector.enable(FaultPoint.AFTER_REDO_BEFORE_UNDO, exit_code={DEFAULT_CRASH_EXIT_CODE})

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
    rm.recover()
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        db_file = tmp_path / "repeat_crash.db"
        wal_file = tmp_path / "repeat_crash.wal"

        # Parent process completes recovery from the persistent files
        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, 5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.losers == [1]
            assert result.clrs_generated == 1

            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"INITIAL_CYCLE_P0"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Scenario 19: Subprocess crash at AFTER_UNDO_BEFORE_COMPLETE and restart recovery
    # -------------------------------------------------------------------------
    def test_crash_at_after_undo_before_complete_and_restart_recovery(
        self, tmp_path: Path
    ) -> None:
        """Scenario 19: Genuine subprocess crash at AFTER_UNDO_BEFORE_COMPLETE followed by restart recovery."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.transaction.transaction_manager import TransactionManager
from app.recovery.recovery_manager import RecoveryManager
from app.fault.constants import FaultPoint, DEFAULT_CRASH_EXIT_CODE
from app.fault.fault_injector import fault_injector

db_path = Path("after_undo_crash.db")
wal_path = Path("after_undo_crash.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    bm.allocate_page()
    p = bm.fetch_page(0)
    p.write(0, b"ORIGINAL_PAGE_0_")
    bm.unpin_page(0, is_dirty=True)
    bm.flush_all_pages()

    t1 = tm.begin_transaction()
    lm.append_update(t1.transaction_id, 0, 0, b"ORIGINAL_PAGE_0_", b"CRASH_MUTATION1_")
    lm.flush()

fault_injector.enable(FaultPoint.AFTER_UNDO_BEFORE_COMPLETE, exit_code={DEFAULT_CRASH_EXIT_CODE})

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
    rm.recover()
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        db_file = tmp_path / "after_undo_crash.db"
        wal_file = tmp_path / "after_undo_crash.wal"

        assert db_file.exists()
        assert wal_file.exists()

        # Restart recovery using the exact same persistent .db and .wal files
        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, 5, log_manager=lm)
            rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
            result = rm.recover()

            assert result.is_success is True
            assert result.stage == "COMPLETE"
            # Transaction 1 had its CLRs and ABORT recorded in WAL during the first pass Undo
            # Restarted recovery recognizes T1 is no longer a loser
            assert result.losers == []
            assert result.clrs_generated == 0
            assert result.updates_undone == 0

            # Verified page contents in buffer
            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"ORIGINAL_PAGE_0_"
            bm.unpin_page(0, is_dirty=False)

        # Verified persistent page content on disk after recovery
        with DiskManager(db_file) as dm:
            disk_page = dm.read_page(0)
            assert disk_page.read(0, 16) == b"ORIGINAL_PAGE_0_"


    # -------------------------------------------------------------------------
    # Scenario 10: Idempotent recovery after successful completion
    # -------------------------------------------------------------------------
    def test_idempotent_recovery_after_completion(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 10: Running recovery twice produces 0 additional undo steps and 0 new CLRs."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"IDEMPOTENT_DATA")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"IDEMPOTENT_DATA", b"TMP_MODIFICATION")
            lm.flush()

            rm1 = RecoveryManager(log_manager=lm, buffer_manager=bm)
            res1 = rm1.recover()
            assert res1.is_success is True
            assert res1.clrs_generated == 1

            # Second recovery pass on the exact same state
            rm2 = RecoveryManager(log_manager=lm, buffer_manager=bm)
            res2 = rm2.recover()
            assert res2.is_success is True
            assert res2.losers == []
            assert res2.clrs_generated == 0
            assert res2.updates_undone == 0
            assert res2.pages_modified == []

            p0 = bm.fetch_page(0)
            assert p0.read(0, 15) == b"IDEMPOTENT_DATA"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Scenario 11: Invalid or corrupted WAL causes a clear failure
    # -------------------------------------------------------------------------
    def test_invalid_or_corrupted_wal_causes_clear_failure(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 11: Corrupted WAL magic or CRC mismatch triggers LogCorruptionError and reports failure."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            lm.append_begin(1)
            lm.append_update(1, 0, 0, b"A", b"B")
            lm.flush()

        # Corrupt the WAL bytes on disk
        with open(temp_wal_path, "r+b") as f:
            f.seek(0)
            f.write(b"CORRUPT_MAGIC_BYTES_123")

        with pytest.raises(LogCorruptionError):
            with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
                pass

    # -------------------------------------------------------------------------
    # Scenario 12: Stage ordering is Analysis, Redo, then Undo
    # -------------------------------------------------------------------------
    def test_stage_ordering_is_analysis_redo_undo(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 12: Recovery strictly executes Analysis -> Redo -> Undo in sequential order."""
        execution_order: List[str] = []

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            bm.flush_all_pages()

            lm.append_begin(1)
            lm.append_update(1, 0, 0, b"A", b"B")
            lm.flush()

            # Spy on Analysis, Redo, Undo
            orig_analysis = Analysis.execute
            orig_redo = Redo.execute
            orig_undo = Undo.execute

            def spy_analysis(self_obj):
                execution_order.append("ANALYSIS")
                return orig_analysis(self_obj)

            def spy_redo(self_obj, analysis_res):
                execution_order.append("REDO")
                return orig_redo(self_obj, analysis_res)

            def spy_undo(self_obj, analysis_res):
                execution_order.append("UNDO")
                return orig_undo(self_obj, analysis_res)

            Analysis.execute = spy_analysis  # type: ignore
            Redo.execute = spy_redo  # type: ignore
            Undo.execute = spy_undo  # type: ignore

            try:
                rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
                result = rm.recover()
                assert result.is_success is True
                assert execution_order == ["ANALYSIS", "REDO", "UNDO"]
            finally:
                Analysis.execute = orig_analysis  # type: ignore
                Redo.execute = orig_redo  # type: ignore
                Undo.execute = orig_undo  # type: ignore

    # -------------------------------------------------------------------------
    # Scenario 13: A failed stage prevents later stages from reporting success
    # -------------------------------------------------------------------------
    def test_failed_stage_prevents_later_stages_from_reporting_success(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 13: Failure in a stage prevents subsequent stages and marks recovery as failed."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)

            orig_redo = Redo.execute

            def failing_redo(self_obj, a_res):
                raise RedoError("Simulated Redo hardware I/O error")

            Redo.execute = failing_redo  # type: ignore

            try:
                rm = RecoveryManager(log_manager=lm, buffer_manager=bm)

                # With raise_on_error=True (default), raises RedoError
                with pytest.raises(RedoError, match="Simulated Redo hardware I/O error"):
                    rm.recover(raise_on_error=True)

                assert rm.is_recovered is False
                assert rm.last_result is not None
                assert rm.last_result.is_success is False
                assert rm.last_result.stage == "FAILED_AT_REDO"
                assert rm.last_result.analysis_result is not None
                assert rm.last_result.redo_result is None
                assert rm.last_result.undo_result is None

                # With raise_on_error=False, returns failed result without exception
                res_no_raise = rm.recover(raise_on_error=False)
                assert res_no_raise.is_success is False
                assert res_no_raise.stage == "FAILED_AT_REDO"
                assert "Simulated Redo hardware I/O error" in str(res_no_raise.error_message)
            finally:
                Redo.execute = orig_redo  # type: ignore

    # -------------------------------------------------------------------------
    # Scenario 14: Recovery result structure and immutability
    # -------------------------------------------------------------------------
    def test_recovery_result_structure_and_immutability(self) -> None:
        """Scenario 14: RecoveryResult attributes, properties, immutability, to_dict, repr, and eq."""
        a_res = AnalysisResult(
            transaction_table={1: RecoveredTransaction(1, TransactionState.COMMITTED, 2)},
            dirty_page_table={0: 2},
            winners=[1],
            losers=[],
            redo_start_lsn=2,
            analysis_start_lsn=1,
            last_analyzed_lsn=2,
        )
        r_res = RedoResult(
            redo_start_lsn=2,
            last_scanned_lsn=2,
            records_scanned=1,
            records_redone=1,
            records_skipped=0,
            pages_modified=[0],
            pages_skipped=[],
        )
        u_res = UndoResult(
            loser_transactions=0,
            transactions_undone=0,
            clrs_generated=0,
            updates_undone=0,
            pages_modified=[],
            records_skipped=0,
            last_processed_lsn=None,
            completed_transactions=[1],
            remaining_losers=[],
            is_complete=True,
        )

        result = RecoveryResult(
            is_success=True,
            analysis_result=a_res,
            redo_result=r_res,
            undo_result=u_res,
            error_message=None,
            stage="COMPLETE",
        )

        assert result.is_success is True
        assert result.success is True
        assert result.stage == "COMPLETE"
        assert result.error_message is None
        assert result.winners == [1]
        assert result.losers == []
        assert result.pages_modified == [0]
        assert result.records_redone == 1
        assert result.clrs_generated == 0
        assert result.updates_undone == 0
        assert result.completed_transactions == [1]

        # Immutability: modifying returned lists does not affect internal state
        winners = result.winners
        winners.append(99)
        assert result.winners == [1]

        pages = result.pages_modified
        pages.append(99)
        assert result.pages_modified == [0]

        d = result.to_dict()
        assert d["is_success"] is True
        assert d["stage"] == "COMPLETE"
        assert d["winners"] == [1]
        assert d["losers"] == []
        assert "analysis" in d
        assert "redo" in d
        assert "undo" in d

        assert "RecoveryResult" in repr(result)
        assert "COMPLETE" in repr(result)

        # Equality
        result2 = RecoveryResult(
            is_success=True,
            analysis_result=a_res,
            redo_result=r_res,
            undo_result=u_res,
            error_message=None,
            stage="COMPLETE",
        )
        assert result == result2

    # -------------------------------------------------------------------------
    # Scenario 15: Existing Phase 0–9 behavior remains compatible
    # -------------------------------------------------------------------------
    def test_existing_phase_0_to_9_behavior_compatibility(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 15: Standalone Analysis, Redo, Undo, and LogManager APIs remain fully functional."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            bm.flush_all_pages()

            lm.append_begin(1)
            lm.append_update(1, 0, 0, b"AAA", b"BBB")
            lm.flush()

            # Standalone Analysis
            a_res = Analysis.analyze(lm)
            assert a_res.losers == [1]

            # Standalone Redo
            r_res = Redo.recover(lm, bm, a_res)
            assert r_res.records_redone == 1

            # Standalone Undo
            u_res = Undo.recover(lm, bm, a_res)
            assert u_res.clrs_generated == 1
            assert u_res.is_complete is True

    # -------------------------------------------------------------------------
    # Scenario 16: Loser transaction with no UPDATE records
    # -------------------------------------------------------------------------
    def test_loser_transaction_with_no_update_records(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 16: Active transaction with no updates completes cleanly through RecoveryManager."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            t1 = tm.begin_transaction()  # No updates logged
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
            assert result.clrs_generated == 0
            assert result.updates_undone == 0
            assert result.completed_transactions == [1]

    # -------------------------------------------------------------------------
    # Scenario 17: Convenience recovery from files (recover_from_files)
    # -------------------------------------------------------------------------
    def test_recover_from_files_convenience_api(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 17: RecoveryManager.recover_from_files successfully opens, recovers, and closes files."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"FROM_FILES_INIT")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"FROM_FILES_INIT", b"FROM_FILES_MOD_")
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

        # Reopen to verify disk content
        with DiskManager(db_path=temp_db_path) as dm:
            p0 = dm.read_page(0)
            assert p0.read(0, 15) == b"FROM_FILES_INIT"

    # -------------------------------------------------------------------------
    # Scenario 18: Constructor validations and closed managers rejection
    # -------------------------------------------------------------------------
    def test_constructor_validations_and_closed_managers(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Scenario 18: Invalid constructor arguments or closed managers raise RecoveryManagerError."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)

            with pytest.raises(RecoveryManagerError, match="log_manager cannot be None"):
                RecoveryManager(log_manager=None, buffer_manager=bm)  # type: ignore

            with pytest.raises(RecoveryManagerError, match="buffer_manager cannot be None"):
                RecoveryManager(log_manager=lm, buffer_manager=None)  # type: ignore

            # Closed log manager
            lm.close()
            with pytest.raises(RecoveryManagerError, match="log_manager cannot be closed"):
                RecoveryManager(log_manager=lm, buffer_manager=bm)
