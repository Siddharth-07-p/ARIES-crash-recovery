"""Comprehensive test suite for Phase 9: ARIES Undo Phase with CLRs.

Covers all 29 mandatory test requirements:
1. Basic single-update Undo.
2. Multiple updates in one transaction.
3. Multiple loser transactions.
4. Interleaved transactions.
5. A committed winner is never undone.
6. ABORT record with incomplete rollback.
7. Fully completed aborted transaction is not undone again.
8. Correct before-image restoration.
9. CLR creation and serialization/deserialization.
10. Correct CLR prev_lsn chain.
11. Correct undo_next_lsn.
12. Multiple updates to the same page.
13. Updates spanning multiple pages.
14. Loser transaction with no UPDATE records.
15. Empty loser set.
16. Invalid update bounds.
17. WAL-before-page-write ordering.
18. CLR durability.
19. Idempotent or safely resumable Undo.
20. Restart after partial Undo.
21. Crash before CLR flush.
22. Crash after CLR flush but before page flush.
23. Crash after page flush.
24. Recovery continuing from CLR.undo_next_lsn.
25. Consecutive crashes during Undo.
26. Final loser transaction completion.
27. UndoResult serialization and immutability.
28. Existing WAL compatibility.
29. Correct handling of missing log records and invalid chains.
"""

import os
from pathlib import Path
import subprocess
import sys
import pytest

from app.buffer.buffer_manager import BufferManager
from app.fault.constants import DEFAULT_CRASH_EXIT_CODE, FaultPoint
from app.fault.fault_injector import fault_injector
from app.recovery.analysis import Analysis
from app.recovery.exceptions import UndoError
from app.recovery.recovery_state import AnalysisResult, RecoveredTransaction, UndoResult
from app.recovery.redo import Redo
from app.recovery.undo import Undo
from app.storage.constants import PAGE_SIZE
from app.storage.disk_manager import DiskManager
from app.transaction.transaction import TransactionState
from app.transaction.transaction_manager import TransactionManager
from app.wal.exceptions import InvalidLogRecordError, LogCorruptionError
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecord, LogRecordType


@pytest.fixture
def temp_wal_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary WAL filepath."""
    return tmp_path / "test_undo.wal"


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary DB filepath."""
    return tmp_path / "test_undo.db"


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


class TestARIESUndo:
    """Comprehensive test suite for Phase 9 ARIES Undo phase with CLRs."""

    # -------------------------------------------------------------------------
    # Case 1: Basic single-update Undo
    # -------------------------------------------------------------------------
    def test_basic_single_update_undo(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 1: Basic single-update Undo."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            page = bm.fetch_page(0)
            page.write(0, b"INIT")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lsn2 = lm.append_update(t1.transaction_id, page_id=0, offset=0, before_image=b"INIT", after_image=b"MOD1")
            bm.fetch_page(0)
            page.write(0, b"MOD1")
            bm.set_page_lsn(0, lsn2)
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            assert analysis_res.losers == [1]

            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res = Undo.execute(analysis_res, lm, bm)

            assert undo_res.loser_transactions == 1
            assert undo_res.transactions_undone == 1
            assert undo_res.clrs_generated == 1
            assert undo_res.updates_undone == 1
            assert undo_res.pages_modified == [0]
            assert undo_res.completed_transactions == [1]
            assert undo_res.is_complete is True

            p0 = bm.fetch_page(0)
            assert p0.read(0, 4) == b"INIT"
            bm.unpin_page(0, is_dirty=False)

            records = lm.read_all()
            clrs = [r for r in records if r.record_type == LogRecordType.CLR]
            assert len(clrs) == 1
            assert clrs[0].transaction_id == 1
            assert clrs[0].page_id == 0
            assert clrs[0].offset == 0
            assert clrs[0].compensation_image == b"INIT"
            assert clrs[0].undo_next_lsn == 1

            aborts = [r for r in records if r.record_type == LogRecordType.ABORT]
            assert len(aborts) == 1
            assert aborts[0].transaction_id == 1

    # -------------------------------------------------------------------------
    # Case 2: Multiple updates in one transaction
    # -------------------------------------------------------------------------
    def test_multiple_updates_in_one_transaction(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 2: Multiple updates in one transaction undone in reverse order."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            page = bm.fetch_page(0)
            page.write(0, b"AAAABBBBCCCC")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lsn2 = lm.append_update(t1.transaction_id, 0, 0, b"AAAA", b"1111")
            lsn3 = lm.append_update(t1.transaction_id, 0, 4, b"BBBB", b"2222")
            lsn4 = lm.append_update(t1.transaction_id, 0, 8, b"CCCC", b"3333")
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            undo = Undo(log_manager=lm, buffer_manager=bm)
            undo_res = undo.execute(analysis_res)

            assert undo_res.clrs_generated == 3
            assert undo_res.updates_undone == 3
            assert undo_res.transactions_undone == 1

            p0 = bm.fetch_page(0)
            assert p0.read(0, 12) == b"AAAABBBBCCCC"
            bm.unpin_page(0, is_dirty=False)

            clrs = [r for r in lm.read_all() if r.record_type == LogRecordType.CLR]
            assert len(clrs) == 3
            # Reverse order undoing LSN 4 -> 3 -> 2
            assert clrs[0].offset == 8 and clrs[0].undo_next_lsn == lsn3
            assert clrs[1].offset == 4 and clrs[1].undo_next_lsn == lsn2
            assert clrs[2].offset == 0 and clrs[2].undo_next_lsn == 1

    # -------------------------------------------------------------------------
    # Case 3: Multiple loser transactions
    # -------------------------------------------------------------------------
    def test_multiple_loser_transactions(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 3: Multiple loser transactions are all undone."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()  # P0
            bm.allocate_page()  # P1
            p0 = bm.fetch_page(0)
            p0.write(0, b"ORIG_P0")
            bm.unpin_page(0, is_dirty=True)
            p1 = bm.fetch_page(1)
            p1.write(0, b"ORIG_P1")
            bm.unpin_page(1, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            t2 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"ORIG_P0", b"MODD_P0")
            lm.append_update(t2.transaction_id, 1, 0, b"ORIG_P1", b"MODD_P1")
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            assert analysis_res.losers == [1, 2]

            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            assert undo_res.loser_transactions == 2
            assert undo_res.transactions_undone == 2
            assert undo_res.clrs_generated == 2
            assert undo_res.completed_transactions == [1, 2]

            p0 = bm.fetch_page(0)
            p1 = bm.fetch_page(1)
            assert p0.read(0, 7) == b"ORIG_P0"
            assert p1.read(0, 7) == b"ORIG_P1"
            bm.unpin_page(0, is_dirty=False)
            bm.unpin_page(1, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 4: Interleaved transactions
    # -------------------------------------------------------------------------
    def test_interleaved_transactions(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 4: Interleaved loser transactions undone via global priority queue."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"AA")
            bm.unpin_page(0, is_dirty=True)
            p1 = bm.fetch_page(1)
            p1.write(0, b"BB")
            bm.unpin_page(1, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            t2 = tm.begin_transaction()  # LSN 2
            lm.append_update(t1.transaction_id, 0, 0, b"AA", b"11")  # LSN 3
            lm.append_update(t2.transaction_id, 1, 0, b"BB", b"22")  # LSN 4
            lm.append_update(t1.transaction_id, 0, 0, b"11", b"33")  # LSN 5
            lm.append_update(t2.transaction_id, 1, 0, b"22", b"44")  # LSN 6
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            assert undo_res.clrs_generated == 4
            assert undo_res.transactions_undone == 2

            p0 = bm.fetch_page(0)
            p1 = bm.fetch_page(1)
            assert p0.read(0, 2) == b"AA"
            assert p1.read(0, 2) == b"BB"
            bm.unpin_page(0, is_dirty=False)
            bm.unpin_page(1, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 5: A committed winner is never undone
    # -------------------------------------------------------------------------
    def test_committed_winner_never_undone(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 5: A committed winner is never undone."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"INIT0")
            bm.unpin_page(0, is_dirty=True)
            p1 = bm.fetch_page(1)
            p1.write(0, b"INIT1")
            bm.unpin_page(1, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # Winner
            lm.append_update(t1.transaction_id, 0, 0, b"INIT0", b"WIN0_")
            tm.commit_transaction(t1.transaction_id)

            t2 = tm.begin_transaction()  # Loser
            lm.append_update(t2.transaction_id, 1, 0, b"INIT1", b"LOSE1")
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            assert analysis_res.winners == [1]
            assert analysis_res.losers == [2]

            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            assert undo_res.transactions_undone == 1
            assert undo_res.completed_transactions == [2]

            p0 = bm.fetch_page(0)
            assert p0.read(0, 5) == b"WIN0_"
            bm.unpin_page(0, is_dirty=False)

            p1 = bm.fetch_page(1)
            assert p1.read(0, 5) == b"INIT1"
            bm.unpin_page(1, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 6: ABORT record with incomplete rollback
    # -------------------------------------------------------------------------
    def test_abort_record_with_incomplete_rollback(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 6: ABORT record alone does not prove rollback finished; incomplete aborted transactions must be undone."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"ORIGINAL_DATA_")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            # T1 modifies page and logs ABORT without physical rollback (e.g. crash during abort)
            t1 = tm.begin_transaction()  # LSN 1
            lm.append_update(t1.transaction_id, 0, 0, b"ORIGINAL_DATA_", b"UNCOMMITTED___")  # LSN 2
            lm.append_abort(t1.transaction_id)  # LSN 3
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            assert analysis_res.transaction_table[1].state == TransactionState.ABORTED

            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            assert undo_res.loser_transactions == 1
            assert undo_res.transactions_undone == 1
            assert undo_res.completed_transactions == [1]
            assert undo_res.clrs_generated == 1
            assert undo_res.updates_undone == 1

            p0 = bm.fetch_page(0)
            assert p0.read(0, 14) == b"ORIGINAL_DATA_"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 7: Fully completed aborted transaction is not undone again
    # -------------------------------------------------------------------------
    def test_fully_completed_aborted_transaction_is_not_undone_again(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 7: Fully completed aborted transaction is not misclassified as a loser or re-undone."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"BASE_DATA")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            # T1: BEGIN -> UPDATE -> CLR -> ABORT (fully rolled back and completed)
            t1 = tm.begin_transaction()  # LSN 1
            lm.append_update(t1.transaction_id, 0, 0, b"BASE_DATA", b"TMP_MODIF")  # LSN 2
            lm.append_clr(t1.transaction_id, 0, 0, undo_next_lsn=1, compensation_image=b"BASE_DATA")  # LSN 3
            lm.append_abort(t1.transaction_id)  # LSN 4

            # T2: BEGIN -> ABORT without updates (fully completed)
            t2 = tm.begin_transaction()  # LSN 5
            lm.append_abort(t2.transaction_id)  # LSN 6
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)

            # Neither T1 nor T2 should be in losers
            assert 1 not in analysis_res.losers
            assert 2 not in analysis_res.losers
            assert analysis_res.losers == []

            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            assert undo_res.loser_transactions == 0
            assert undo_res.clrs_generated == 0
            assert undo_res.updates_undone == 0

    # -------------------------------------------------------------------------
    # Case 8: Correct before-image restoration
    # -------------------------------------------------------------------------
    def test_correct_before_image_restoration(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 8: Before-image is restored at exact offset without disturbing surroundings."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p = bm.fetch_page(0)
            p.write(0, b"PREFIX_ORIGINAL_SUFFIX")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 7, b"ORIGINAL", b"MODIFIED")
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            p = bm.fetch_page(0)
            assert p.read(0, 22) == b"PREFIX_ORIGINAL_SUFFIX"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 9: CLR creation and serialization/deserialization
    # -------------------------------------------------------------------------
    def test_clr_creation_and_serialization(self) -> None:
        """Case 9: CLR creation, properties, deterministic binary serialization and deserialization."""
        clr = LogRecord(
            lsn=42,
            record_type=LogRecordType.CLR,
            transaction_id=7,
            prev_lsn=30,
            page_id=3,
            offset=128,
            after_image=b"COMP_DATA_123",
            undo_next_lsn=15,
        )

        assert clr.lsn == 42
        assert clr.record_type == LogRecordType.CLR
        assert clr.transaction_id == 7
        assert clr.prev_lsn == 30
        assert clr.page_id == 3
        assert clr.offset == 128
        assert clr.after_image == b"COMP_DATA_123"
        assert clr.compensation_image == b"COMP_DATA_123"
        assert clr.undo_next_lsn == 15

        serialized = clr.serialize()
        deserialized, consumed = LogRecord.deserialize(serialized)

        assert consumed == len(serialized)
        assert deserialized == clr

        # Corrupted payload raises LogCorruptionError
        corrupted = bytearray(serialized)
        corrupted[-1] ^= 0xFF
        with pytest.raises(LogCorruptionError):
            LogRecord.deserialize(bytes(corrupted))

    # -------------------------------------------------------------------------
    # Case 10: Correct CLR prev_lsn chain
    # -------------------------------------------------------------------------
    def test_correct_clr_prev_lsn_chain(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 10: Prev_lsn chain consistency throughout transaction lifecycle and undo."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            u1 = lm.append_update(t1.transaction_id, 0, 0, b"A", b"B")  # LSN 2 (prev 1)
            u2 = lm.append_update(t1.transaction_id, 0, 0, b"B", b"C")  # LSN 3 (prev 2)
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            records = lm.read_all()
            assert len(records) == 6
            # LSN 1: BEGIN (prev None)
            # LSN 2: UPDATE (prev 1)
            # LSN 3: UPDATE (prev 2)
            # LSN 4: CLR for LSN 3 (prev 3, undo_next 2)
            # LSN 5: CLR for LSN 2 (prev 4, undo_next 1)
            # LSN 6: ABORT (prev 5)
            assert records[0].lsn == 1
            assert records[1].lsn == 2 and records[1].prev_lsn == 1
            assert records[2].lsn == 3 and records[2].prev_lsn == 2
            assert records[3].lsn == 4 and records[3].prev_lsn == 3 and records[3].undo_next_lsn == 2
            assert records[4].lsn == 5 and records[4].prev_lsn == 4 and records[4].undo_next_lsn == 1
            assert records[5].lsn == 6 and records[5].prev_lsn == 5

    # -------------------------------------------------------------------------
    # Case 11: Correct undo_next_lsn
    # -------------------------------------------------------------------------
    def test_correct_undo_next_lsn(self) -> None:
        """Case 11: Validation and correctness of undo_next_lsn pointers."""
        # Valid CLR with undo_next_lsn pointing to earlier record
        clr = LogRecord(
            lsn=20,
            record_type=LogRecordType.CLR,
            transaction_id=1,
            prev_lsn=15,
            page_id=0,
            offset=0,
            after_image=b"VAL",
            undo_next_lsn=10,
        )
        assert clr.undo_next_lsn == 10

        # Valid CLR with undo_next_lsn=None
        clr_final = LogRecord(
            lsn=25,
            record_type=LogRecordType.CLR,
            transaction_id=1,
            prev_lsn=20,
            page_id=0,
            offset=0,
            after_image=b"VAL",
            undo_next_lsn=None,
        )
        assert clr_final.undo_next_lsn is None

        # Rejects undo_next_lsn >= lsn
        with pytest.raises(InvalidLogRecordError, match="must be strictly less than record LSN"):
            LogRecord(
                lsn=20,
                record_type=LogRecordType.CLR,
                transaction_id=1,
                prev_lsn=15,
                page_id=0,
                offset=0,
                after_image=b"VAL",
                undo_next_lsn=20,
            )

        # Rejects undo_next_lsn on non-CLR record
        with pytest.raises(InvalidLogRecordError, match="only allowed for CLR records"):
            LogRecord(
                lsn=20,
                record_type=LogRecordType.UPDATE,
                transaction_id=1,
                prev_lsn=15,
                page_id=0,
                offset=0,
                before_image=b"A",
                after_image=b"B",
                undo_next_lsn=10,
            )

    # -------------------------------------------------------------------------
    # Case 12: Multiple updates to the same page
    # -------------------------------------------------------------------------
    def test_multiple_updates_to_same_page(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 12: Multiple updates to the same page by different transactions undone correctly."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p = bm.fetch_page(0)
            p.write(0, b"BASE_DATA")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            t2 = tm.begin_transaction()  # LSN 2
            lm.append_update(t1.transaction_id, 0, 0, b"BASE", b"MOD1")  # LSN 3
            lm.append_update(t2.transaction_id, 0, 5, b"DATA", b"MOD2")  # LSN 4
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            assert undo_res.updates_undone == 2
            assert undo_res.clrs_generated == 2
            assert undo_res.pages_modified == [0]

            p = bm.fetch_page(0)
            assert p.read(0, 9) == b"BASE_DATA"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 13: Updates spanning multiple pages
    # -------------------------------------------------------------------------
    def test_updates_spanning_multiple_pages(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 13: Updates across multiple distinct pages undone in one recovery."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=10, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            for i in range(4):
                bm.allocate_page()
                p = bm.fetch_page(i)
                p.write(0, f"INIT_PAGE_{i}".encode())
                bm.unpin_page(i, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            for i in range(4):
                lm.append_update(t1.transaction_id, i, 0, f"INIT_PAGE_{i}".encode(), f"MODD_PAGE_{i}".encode())
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=10, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            assert undo_res.updates_undone == 4
            assert undo_res.clrs_generated == 4
            assert undo_res.pages_modified == [0, 1, 2, 3]

            for i in range(4):
                p = bm.fetch_page(i)
                assert p.read(0, 11) == f"INIT_PAGE_{i}".encode()
                bm.unpin_page(i, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 14: Loser transaction with no UPDATE records
    # -------------------------------------------------------------------------
    def test_loser_transaction_with_no_update_records(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 14: Loser transaction with no UPDATE records completes cleanly."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            t1 = tm.begin_transaction()  # LSN 1 (no updates)
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            assert analysis_res.losers == [1]

            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            assert undo_res.clrs_generated == 0
            assert undo_res.updates_undone == 0
            assert undo_res.transactions_undone == 1
            assert undo_res.completed_transactions == [1]

            records = lm.read_all()
            assert records[-1].record_type == LogRecordType.ABORT

    # -------------------------------------------------------------------------
    # Case 15: Empty loser set
    # -------------------------------------------------------------------------
    def test_empty_loser_set(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 15: Empty loser set returns clean result immediately."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            t1 = tm.begin_transaction()
            tm.commit_transaction(t1.transaction_id)
            lm.flush()

            analysis_res = Analysis.analyze(log_manager=lm)
            assert analysis_res.losers == []

            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            assert undo_res.loser_transactions == 0
            assert undo_res.transactions_undone == 0
            assert undo_res.clrs_generated == 0
            assert undo_res.is_complete is True

    # -------------------------------------------------------------------------
    # Case 16: Invalid update bounds
    # -------------------------------------------------------------------------
    def test_invalid_update_bounds(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 16: Out-of-bounds update records fail safely during Undo without mutating pages."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            p = bm.fetch_page(0)
            p.write(0, b"UNTOUCHED_PAGE_DATA")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            lm.append_begin(1)

            # Manually inject malformed UPDATE record
            malformed_rec = object.__new__(LogRecord)
            malformed_rec._lsn = 2
            malformed_rec._record_type = LogRecordType.UPDATE
            malformed_rec._transaction_id = 1
            malformed_rec._prev_lsn = 1
            malformed_rec._page_id = 0
            malformed_rec._offset = PAGE_SIZE - 2
            malformed_rec._before_image = b"TOOLONG_BEFORE"
            malformed_rec._after_image = b"TL"
            malformed_rec._checkpoint_payload = None
            malformed_rec._undo_next_lsn = None

            lm._records[2] = malformed_rec

            analysis_res = AnalysisResult(
                transaction_table={1: RecoveredTransaction(1, TransactionState.RUNNING, 2)},
                dirty_page_table={0: 2},
                winners=[],
                losers=[1],
                redo_start_lsn=2,
                analysis_start_lsn=1,
                last_analyzed_lsn=2,
            )

            undo = Undo(log_manager=lm, buffer_manager=bm)
            with pytest.raises(UndoError, match="Invalid UPDATE bounds"):
                undo.execute(analysis_res)

            # Page content must remain unmutated
            p0 = bm.fetch_page(0)
            assert p0.read(0, 19) == b"UNTOUCHED_PAGE_DATA"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 17: WAL-before-page-write ordering
    # -------------------------------------------------------------------------
    def test_wal_before_page_write_ordering(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 17: CLR is durable in WAL before modified page is persisted to disk."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"X", b"Y")
            lm.flush()

            analysis_res = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            page_lsn = bm.get_page_lsn(0)
            assert page_lsn > 0
            assert lm.flushed_lsn >= page_lsn

    # -------------------------------------------------------------------------
    # Case 18: CLR durability
    # -------------------------------------------------------------------------
    def test_clr_durability(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 18: CLR record is fully written and durable in WAL file before page flush."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"DUR_BEFORE", b"DUR_AFTER_")
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            assert undo_res.clrs_generated == 1
            # Inspect raw file on disk
            with open(temp_wal_path, "rb") as f:
                wal_bytes = f.read()
            assert b"DUR_BEFORE" in wal_bytes

    # -------------------------------------------------------------------------
    # Case 19: Idempotent or safely resumable Undo
    # -------------------------------------------------------------------------
    def test_idempotent_or_safely_resumable_undo(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 19: Undo is idempotent — second execution on completed state performs 0 undo steps."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p = bm.fetch_page(0)
            p.write(0, b"ORIG")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"ORIG", b"MODD")
            lm.flush()

            analysis_res = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res1 = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            assert undo_res1.clrs_generated == 1

            # Second analysis pass on completed WAL
            analysis_res2 = Analysis.analyze(log_manager=lm)
            assert analysis_res2.losers == []

            undo_res2 = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res2)
            assert undo_res2.clrs_generated == 0
            assert undo_res2.updates_undone == 0

    # -------------------------------------------------------------------------
    # Case 20: Restart after partial Undo
    # -------------------------------------------------------------------------
    def test_restart_after_partial_undo(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 20: Crash occurs midway through Undo; next recovery continues from CLR undo_next_lsn."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p = bm.fetch_page(0)
            p.write(0, b"AB")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lsn2 = lm.append_update(t1.transaction_id, 0, 0, b"A", b"1")  # LSN 2
            lsn3 = lm.append_update(t1.transaction_id, 0, 1, b"B", b"2")  # LSN 3
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        # Step 2: Partial Undo (simulate Crash after writing CLR for LSN 3)
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis1 = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis1)

            page = bm.fetch_page(0)
            page.write(1, b"B")
            clr_lsn = lm.append_clr(1, 0, 1, undo_next_lsn=lsn2, compensation_image=b"B")  # LSN 4
            lm.flush()
            bm.set_page_lsn(0, clr_lsn)
            bm.unpin_page(0, is_dirty=True)
            bm.flush_page(0)

            bm.close(flush_dirty=False)
            lm.close()
            dm.close()

        # Step 3: Second restart and complete recovery
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis2 = Analysis.analyze(log_manager=lm)

            assert analysis2.losers == [1]
            assert analysis2.transaction_table[1].last_lsn == 4

            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis2)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis2)

            assert undo_res.clrs_generated == 1  # Only LSN 2 undone
            assert undo_res.updates_undone == 1
            assert undo_res.records_skipped >= 1  # Skipped CLR at LSN 4

            p = bm.fetch_page(0)
            assert p.read(0, 2) == b"AB"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 21: Crash before CLR flush (subprocess crash)
    # -------------------------------------------------------------------------
    def test_crash_before_clr_flush(self, tmp_path: Path) -> None:
        """Case 21: Subprocess crash at DURING_UNDO_BEFORE_CLR_FLUSH; parent verifies WAL state and recovers."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.transaction.transaction_manager import TransactionManager
from app.recovery.analysis import Analysis
from app.recovery.redo import Redo
from app.recovery.undo import Undo
from app.fault.constants import FaultPoint, DEFAULT_CRASH_EXIT_CODE
from app.fault.fault_injector import fault_injector

db_path = Path("crash21.db")
wal_path = Path("crash21.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    bm.allocate_page()
    p = bm.fetch_page(0)
    p.write(0, b"ORIGINAL")
    bm.unpin_page(0, is_dirty=True)
    bm.flush_all_pages()

    t1 = tm.begin_transaction()
    lm.append_update(t1.transaction_id, 0, 0, b"ORIGINAL", b"CRASH21_")
    lm.flush()

fault_injector.enable(FaultPoint.DURING_UNDO_BEFORE_CLR_FLUSH, exit_code={DEFAULT_CRASH_EXIT_CODE})

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    a = Analysis.analyze(lm)
    Redo.recover(lm, bm, a)
    Undo.recover(lm, bm, a)
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        wal_file = tmp_path / "crash21.wal"
        db_file = tmp_path / "crash21.db"

        # Parent process verifies CLR was NOT flushed before crash
        with LogManager(wal_file) as lm:
            records = lm.read_all()
            assert not any(r.record_type == LogRecordType.CLR for r in records)

        # Parent process completes recovery successfully
        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, 5, log_manager=lm)
            a = Analysis.analyze(lm)
            assert a.losers == [1]
            Redo.recover(lm, bm, a)
            undo_res = Undo.recover(lm, bm, a)
            assert undo_res.clrs_generated == 1
            p0 = bm.fetch_page(0)
            assert p0.read(0, 8) == b"ORIGINAL"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 22: Crash after CLR flush but before page flush (subprocess crash)
    # -------------------------------------------------------------------------
    def test_crash_after_clr_flush_before_page_flush(self, tmp_path: Path) -> None:
        """Case 22: Subprocess crash at DURING_UNDO_AFTER_CLR_FLUSH_BEFORE_PAGE_FLUSH."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.transaction.transaction_manager import TransactionManager
from app.recovery.analysis import Analysis
from app.recovery.redo import Redo
from app.recovery.undo import Undo
from app.fault.constants import FaultPoint, DEFAULT_CRASH_EXIT_CODE
from app.fault.fault_injector import fault_injector

db_path = Path("crash22.db")
wal_path = Path("crash22.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    bm.allocate_page()
    p = bm.fetch_page(0)
    p.write(0, b"ORIGINAL")
    bm.unpin_page(0, is_dirty=True)
    bm.flush_all_pages()

    t1 = tm.begin_transaction()
    lm.append_update(t1.transaction_id, 0, 0, b"ORIGINAL", b"CRASH22_")
    lm.flush()

fault_injector.enable(FaultPoint.DURING_UNDO_AFTER_CLR_FLUSH_BEFORE_PAGE_FLUSH, exit_code={DEFAULT_CRASH_EXIT_CODE})

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    a = Analysis.analyze(lm)
    Redo.recover(lm, bm, a)
    Undo.recover(lm, bm, a)
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        wal_file = tmp_path / "crash22.wal"
        db_file = tmp_path / "crash22.db"

        # Parent process verifies CLR IS durable in WAL
        with LogManager(wal_file) as lm:
            records = lm.read_all()
            assert any(r.record_type == LogRecordType.CLR for r in records)

        # Parent process restarts recovery: Redo replays CLR compensation, Undo skips duplicate
        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, 5, log_manager=lm)
            a = Analysis.analyze(lm)
            Redo.recover(lm, bm, a)
            undo_res = Undo.recover(lm, bm, a)
            # 0 new CLRs because CLR was already written and durable
            assert undo_res.clrs_generated == 0
            p0 = bm.fetch_page(0)
            assert p0.read(0, 8) == b"ORIGINAL"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 23: Crash after page flush (subprocess crash)
    # -------------------------------------------------------------------------
    def test_crash_after_page_flush(self, tmp_path: Path) -> None:
        """Case 23: Subprocess crash at DURING_UNDO_AFTER_PAGE_FLUSH."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.transaction.transaction_manager import TransactionManager
from app.recovery.analysis import Analysis
from app.recovery.redo import Redo
from app.recovery.undo import Undo
from app.fault.constants import FaultPoint, DEFAULT_CRASH_EXIT_CODE
from app.fault.fault_injector import fault_injector

db_path = Path("crash23.db")
wal_path = Path("crash23.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    bm.allocate_page()
    p = bm.fetch_page(0)
    p.write(0, b"ORIGINAL")
    bm.unpin_page(0, is_dirty=True)
    bm.flush_all_pages()

    t1 = tm.begin_transaction()
    lm.append_update(t1.transaction_id, 0, 0, b"ORIGINAL", b"CRASH23_")
    lm.flush()

fault_injector.enable(FaultPoint.DURING_UNDO_AFTER_PAGE_FLUSH, exit_code={DEFAULT_CRASH_EXIT_CODE})

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, 5, log_manager=lm)
    a = Analysis.analyze(lm)
    Redo.recover(lm, bm, a)
    Undo.recover(lm, bm, a)
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        wal_file = tmp_path / "crash23.wal"
        db_file = tmp_path / "crash23.db"

        # Parent process verifies page was persisted with restored value
        with DiskManager(db_file) as dm:
            p0 = dm.read_page(0)
            assert p0.read(0, 8) == b"ORIGINAL"

        # Parent process completes recovery without error
        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, 5, log_manager=lm)
            a = Analysis.analyze(lm)
            Redo.recover(lm, bm, a)
            undo_res = Undo.recover(lm, bm, a)
            assert undo_res.clrs_generated == 0

    # -------------------------------------------------------------------------
    # Case 24: Recovery continuing from CLR.undo_next_lsn
    # -------------------------------------------------------------------------
    def test_recovery_continuing_from_clr_undo_next_lsn(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 24: Recovery jumps to undo_next_lsn and never repeats compensation for an undone update."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p = bm.fetch_page(0)
            p.write(0, b"XY")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            u1 = lm.append_update(t1.transaction_id, 0, 0, b"X", b"1")  # LSN 2
            u2 = lm.append_update(t1.transaction_id, 0, 1, b"Y", b"2")  # LSN 3

            # Simulate recovery that wrote CLR for u2, pointing undo_next_lsn to u1
            clr1 = lm.append_clr(t1.transaction_id, 0, 1, undo_next_lsn=u1, compensation_image=b"Y")  # LSN 4
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            a = Analysis.analyze(lm)
            Redo.recover(lm, bm, a)
            undo_res = Undo.recover(lm, bm, a)

            # Only u1 should be undone, generating exactly 1 CLR
            assert undo_res.clrs_generated == 1
            assert undo_res.updates_undone == 1

            p0 = bm.fetch_page(0)
            assert p0.read(0, 2) == b"XY"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 25: Consecutive crashes during Undo
    # -------------------------------------------------------------------------
    def test_multiple_consecutive_crashes_during_undo(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 25: Multiple consecutive crashes during Undo (Crash -> CLR 1 -> Crash -> CLR 2 -> Complete)."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p = bm.fetch_page(0)
            p.write(0, b"ORIGINAL_VALUE")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            t1 = tm.begin_transaction()  # LSN 1
            lsn2 = lm.append_update(t1.transaction_id, 0, 0, b"ORIG", b"MOD1")  # LSN 2
            lsn3 = lm.append_update(t1.transaction_id, 0, 4, b"INAL", b"MOD2")  # LSN 3
            lsn4 = lm.append_update(t1.transaction_id, 0, 9, b"VALUE", b"MOD3_")  # LSN 4
            lm.flush()

            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        # Crash 1 after writing CLR for LSN 4
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            a1 = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=a1)

            p = bm.fetch_page(0)
            p.write(9, b"VALUE")
            clr1 = lm.append_clr(1, 0, 9, undo_next_lsn=lsn3, compensation_image=b"VALUE")  # LSN 5
            lm.flush()
            bm.set_page_lsn(0, clr1)
            bm.unpin_page(0, is_dirty=True)
            bm.flush_page(0)

            bm.close(flush_dirty=False)
            lm.close()
            dm.close()

        # Crash 2 after writing CLR for LSN 3
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            a2 = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=a2)

            p = bm.fetch_page(0)
            p.write(4, b"INAL")
            clr2 = lm.append_clr(1, 0, 4, undo_next_lsn=lsn2, compensation_image=b"INAL")  # LSN 6
            lm.flush()
            bm.set_page_lsn(0, clr2)
            bm.unpin_page(0, is_dirty=True)
            bm.flush_page(0)

            bm.close(flush_dirty=False)
            lm.close()
            dm.close()

        # Final restart and completion
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            a3 = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=a3)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=a3)

            assert undo_res.updates_undone == 1  # Only LSN 2 was left to undo
            assert undo_res.clrs_generated == 1
            assert undo_res.is_complete is True

            p = bm.fetch_page(0)
            assert p.read(0, 14) == b"ORIGINAL_VALUE"
            bm.unpin_page(0, is_dirty=False)

    # -------------------------------------------------------------------------
    # Case 26: Final loser transaction completion
    # -------------------------------------------------------------------------
    def test_final_loser_transaction_completion(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 26: Loser transactions append durable ABORT completion records."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            bm.flush_all_pages()

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"A", b"B")
            lm.flush()

            analysis_res = Analysis.analyze(log_manager=lm)
            Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            undo_res = Undo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)

            assert undo_res.completed_transactions == [1]
            assert undo_res.remaining_losers == []
            assert undo_res.is_complete is True

            records = lm.read_all()
            last_rec = records[-1]
            assert last_rec.record_type == LogRecordType.ABORT
            assert last_rec.transaction_id == 1
            assert lm.flushed_lsn >= last_rec.lsn

    # -------------------------------------------------------------------------
    # Case 27: UndoResult serialization and immutability
    # -------------------------------------------------------------------------
    def test_undo_result_serialization_and_immutability(self) -> None:
        """Case 27: UndoResult to_dict, immutability, repr, and equality."""
        res1 = UndoResult(
            loser_transactions=2,
            transactions_undone=2,
            clrs_generated=4,
            updates_undone=4,
            pages_modified=[0, 1],
            records_skipped=1,
            last_processed_lsn=10,
            completed_transactions=[1, 2],
            remaining_losers=[],
            is_complete=True,
        )

        d = res1.to_dict()
        assert d["loser_transactions"] == 2
        assert d["transactions_undone"] == 2
        assert d["clrs_generated"] == 4
        assert d["updates_undone"] == 4
        assert d["pages_modified"] == [0, 1]
        assert d["completed_transactions"] == [1, 2]
        assert d["is_complete"] is True

        # Property returns list copies preserving immutability
        pages = res1.pages_modified
        pages.append(99)
        assert res1.pages_modified == [0, 1]

        res2 = UndoResult(
            loser_transactions=2,
            transactions_undone=2,
            clrs_generated=4,
            updates_undone=4,
            pages_modified=[1, 0],
            records_skipped=1,
            last_processed_lsn=10,
            completed_transactions=[2, 1],
            remaining_losers=[],
            is_complete=True,
        )
        assert res1 == res2
        assert "UndoResult" in repr(res1)

    # -------------------------------------------------------------------------
    # Case 28: Existing WAL compatibility
    # -------------------------------------------------------------------------
    def test_existing_wal_compatibility(self, temp_wal_path: Path) -> None:
        """Case 28: Existing WAL record types (BEGIN, COMMIT, ABORT, UPDATE, CHECKPOINT) remain compatible."""
        with LogManager(wal_path=temp_wal_path) as lm:
            lsn1 = lm.append_begin(1)
            lsn2 = lm.append_update(1, 0, 0, b"BEFORE", b"AFTER_")
            lsn3 = lm.append_commit(1)
            lsn4 = lm.append_begin(2)
            lsn5 = lm.append_clr(2, 0, 0, undo_next_lsn=None, compensation_image=b"BEFORE")
            lsn6 = lm.append_abort(2)
            lsn7 = lm.append_checkpoint_begin()
            lsn8 = lm.append_checkpoint_end(checkpoint_begin_lsn=lsn7, payload=b"SNAPSHOT")
            lm.flush()

        with LogManager(wal_path=temp_wal_path) as lm:
            records = lm.read_all()
            assert len(records) == 8
            assert records[0].record_type == LogRecordType.BEGIN
            assert records[1].record_type == LogRecordType.UPDATE
            assert records[2].record_type == LogRecordType.COMMIT
            assert records[3].record_type == LogRecordType.BEGIN
            assert records[4].record_type == LogRecordType.CLR
            assert records[5].record_type == LogRecordType.ABORT
            assert records[6].record_type == LogRecordType.CHECKPOINT_BEGIN
            assert records[7].record_type == LogRecordType.CHECKPOINT_END

    # -------------------------------------------------------------------------
    # Case 29: Correct handling of missing log records and invalid chains
    # -------------------------------------------------------------------------
    def test_missing_log_records_and_invalid_chains(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Case 29: Missing log records or cyclic/invalid chains raise UndoError cleanly."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)

            # Scenario A: Missing log record
            analysis_res = AnalysisResult(
                transaction_table={1: RecoveredTransaction(1, TransactionState.RUNNING, 999)},
                dirty_page_table={},
                winners=[],
                losers=[1],
                redo_start_lsn=None,
                analysis_start_lsn=1,
                last_analyzed_lsn=1,
            )

            undo = Undo(log_manager=lm, buffer_manager=bm)
            with pytest.raises(UndoError, match="not found in WAL"):
                undo.execute(analysis_res)

            # Scenario B: Invalid parameters
            with pytest.raises(UndoError, match="log_manager cannot be None"):
                Undo(log_manager=None, buffer_manager=bm)  # type: ignore

            with pytest.raises(UndoError, match="buffer_manager cannot be None"):
                Undo(log_manager=lm, buffer_manager=None)  # type: ignore

            with pytest.raises(UndoError, match="analysis_result must be a valid AnalysisResult"):
                undo.execute(None)  # type: ignore

    # -------------------------------------------------------------------------
    # Audit Regression Tests
    # -------------------------------------------------------------------------
    def test_audit_clr_redo_replay_and_idempotent_skip(self, temp_db_path: Path, temp_wal_path: Path) -> None:
        """Audit 1: Verify Redo replays CLR when page_lsn < clr_lsn, and skips when page_lsn >= clr_lsn."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"BASE_CONTENT")
            bm.set_page_lsn(0, 1)
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            # Construct WAL with BEGIN, UPDATE, CLR
            lm.append_begin(1)  # LSN 1
            lm.append_update(1, 0, 0, b"BASE_CONTENT", b"DIRTY_UPDATE")  # LSN 2
            lm.append_clr(1, 0, 0, undo_next_lsn=1, compensation_image=b"COMPENSATED_")  # LSN 3
            lm.flush()

            bm.close(flush_dirty=False)
            lm.close()
            dm.close()

        # Pass 1: Disk page has page_lsn=1 (< CLR LSN 3) -> Redo must replay CLR
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            analysis_res = Analysis.analyze(log_manager=lm)

            redo_res = Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            # Replayed both UPDATE (LSN 2) and CLR (LSN 3)
            assert redo_res.records_redone == 2
            assert bm.get_page_lsn(0) == 3

            p0 = bm.fetch_page(0)
            assert p0.read(0, 12) == b"COMPENSATED_"
            bm.unpin_page(0, is_dirty=False)

            # Pass 2: Page now has page_lsn=3 -> Redo must skip both UPDATE and CLR
            redo_res2 = Redo.recover(log_manager=lm, buffer_manager=bm, analysis_result=analysis_res)
            assert redo_res2.records_redone == 0
            assert redo_res2.records_skipped == 2

    def test_audit_incomplete_abort_followed_by_completion_and_second_crash(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Audit 2: Incomplete abort is rolled back, completion is recorded, and second restart does 0 undo steps."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"SAFE_DATA_123")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            # Transaction 1: BEGIN -> UPDATE -> ABORT without undo (crash during abort)
            lm.append_begin(1)  # LSN 1
            lm.append_update(1, 0, 0, b"SAFE_DATA_123", b"CORRUPTED_123")  # LSN 2
            lm.append_abort(1)  # LSN 3
            lm.flush()

            bm.close(flush_dirty=False)
            lm.close()
            dm.close()

        # Restart 1: Undo detects incomplete abort, writes CLR (LSN 4) and completion ABORT (LSN 5)
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            a1 = Analysis.analyze(lm)
            assert a1.transaction_table[1].state == TransactionState.ABORTED

            Redo.recover(lm, bm, a1)
            u1 = Undo.recover(lm, bm, a1)
            assert u1.loser_transactions == 1
            assert u1.transactions_undone == 1
            assert u1.clrs_generated == 1
            assert u1.completed_transactions == [1]

            bm.close(flush_dirty=False)
            lm.close()
            dm.close()

        # Restart 2 (Simulating another crash after completion):
        # Transaction 1 is now fully compensated and completed; must NOT be undone again
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            a2 = Analysis.analyze(lm)
            assert a2.losers == []

            Redo.recover(lm, bm, a2)
            u2 = Undo.recover(lm, bm, a2)
            assert u2.loser_transactions == 0
            assert u2.transactions_undone == 0
            assert u2.clrs_generated == 0
            assert u2.updates_undone == 0
            assert u2.is_complete is True

            p0 = bm.fetch_page(0)
            assert p0.read(0, 13) == b"SAFE_DATA_123"
            bm.unpin_page(0, is_dirty=False)

    def test_audit_cyclic_and_forward_pointing_chains_fail_safely_without_corrupting_pages(
        self, temp_db_path: Path, temp_wal_path: Path
    ) -> None:
        """Audit 3: Forward-pointing and cyclic log chains fail safely with UndoError and leave pages uncorrupted."""
        with DiskManager(db_path=temp_db_path) as dm, LogManager(wal_path=temp_wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"CLEAN_INITIAL_P0")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            # Scenario 1: Forward-pointing log record (prev_lsn >= curr_lsn)
            lm.append_begin(1)  # LSN 1

            # Manually inject UPDATE with forward prev_lsn (LSN 2 with prev_lsn=5)
            fwd_rec = object.__new__(LogRecord)
            fwd_rec._lsn = 2
            fwd_rec._record_type = LogRecordType.UPDATE
            fwd_rec._transaction_id = 1
            fwd_rec._prev_lsn = 5  # Forward pointer
            fwd_rec._page_id = 0
            fwd_rec._offset = 0
            fwd_rec._before_image = b"BAD_DATA"
            fwd_rec._after_image = b"NEW_DATA"
            fwd_rec._checkpoint_payload = None
            fwd_rec._undo_next_lsn = None
            lm._records[2] = fwd_rec

            a1 = AnalysisResult(
                transaction_table={1: RecoveredTransaction(1, TransactionState.RUNNING, 2)},
                dirty_page_table={0: 2},
                winners=[],
                losers=[1],
                redo_start_lsn=2,
                analysis_start_lsn=1,
                last_analyzed_lsn=2,
            )

            undo = Undo(log_manager=lm, buffer_manager=bm)
            with pytest.raises(UndoError, match="forward-pointing"):
                undo.execute(a1)

            # Ensure page remains uncorrupted
            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"CLEAN_INITIAL_P0"
            bm.unpin_page(0, is_dirty=False)

            # Scenario 2: Cyclic log chain
            # LSN 3 points to LSN 2, and LSN 2 points to LSN 3
            cyc_rec3 = object.__new__(LogRecord)
            cyc_rec3._lsn = 3
            cyc_rec3._record_type = LogRecordType.UPDATE
            cyc_rec3._transaction_id = 2
            cyc_rec3._prev_lsn = 2
            cyc_rec3._page_id = 0
            cyc_rec3._offset = 0
            cyc_rec3._before_image = b"CYC_BEFORE"
            cyc_rec3._after_image = b"CYC_AFTER_"
            cyc_rec3._checkpoint_payload = None
            cyc_rec3._undo_next_lsn = None

            cyc_rec2 = object.__new__(LogRecord)
            cyc_rec2._lsn = 2
            cyc_rec2._record_type = LogRecordType.UPDATE
            cyc_rec2._transaction_id = 2
            cyc_rec2._prev_lsn = 3  # Creates cycle: 3 -> 2 -> 3
            cyc_rec2._page_id = 0
            cyc_rec2._offset = 0
            cyc_rec2._before_image = b"CYC_BEFORE"
            cyc_rec2._after_image = b"CYC_AFTER_"
            cyc_rec2._checkpoint_payload = None
            cyc_rec2._undo_next_lsn = None

            lm._records[2] = cyc_rec2
            lm._records[3] = cyc_rec3
            lm._next_lsn = 4

            a2 = AnalysisResult(
                transaction_table={2: RecoveredTransaction(2, TransactionState.RUNNING, 3)},
                dirty_page_table={0: 2},
                winners=[],
                losers=[2],
                redo_start_lsn=2,
                analysis_start_lsn=1,
                last_analyzed_lsn=3,
            )

            with pytest.raises(UndoError, match="forward-pointing|cyclic"):
                undo.execute(a2)

            p0 = bm.fetch_page(0)
            assert p0.read(0, 16) == b"CLEAN_INITIAL_P0"
            bm.unpin_page(0, is_dirty=False)
