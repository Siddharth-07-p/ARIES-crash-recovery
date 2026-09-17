"""Comprehensive test suite for Phase 6: Controlled Crash & Fault Injection."""

import os
from pathlib import Path
import subprocess
import sys
import pytest

from app.fault.constants import DEFAULT_CRASH_EXIT_CODE, FaultPoint
from app.fault.exceptions import InvalidFaultPointError
from app.fault.fault_injector import FaultInjector, fault_injector, fault_scope
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.transaction.transaction_manager import TransactionManager
from app.buffer.buffer_manager import BufferManager


class TestFaultInjectorUnit:
    """Unit tests for FaultInjector configuration, state transitions, and validation."""

    def test_disabled_by_default(self) -> None:
        """Test 1: FaultInjector is disabled by default and does not trigger."""
        injector = FaultInjector()
        assert not injector.is_enabled
        assert injector.configured_fault_point is None
        assert injector.exit_code == DEFAULT_CRASH_EXIT_CODE
        assert injector.trigger(FaultPoint.AFTER_LOG_APPEND_BEFORE_WAL_FLUSH) is False

    def test_enable_and_disable(self) -> None:
        """Test 2: Enabling and disabling fault points."""
        injector = FaultInjector()
        injector.enable(FaultPoint.AFTER_WAL_FLUSH_BEFORE_PAGE_FLUSH, exit_code=99)
        assert injector.is_enabled
        assert injector.configured_fault_point == FaultPoint.AFTER_WAL_FLUSH_BEFORE_PAGE_FLUSH
        assert injector.exit_code == 99

        injector.disable()
        assert not injector.is_enabled
        assert injector.configured_fault_point is None

    def test_string_fault_point_normalization(self) -> None:
        """Test 3: String identifiers normalize to FaultPoint enum."""
        injector = FaultInjector()
        injector.enable("DURING_TRANSACTION_COMMIT")
        assert injector.configured_fault_point == FaultPoint.DURING_TRANSACTION_COMMIT

        with pytest.raises(InvalidFaultPointError):
            injector.enable("INVALID_FAULT_POINT_NAME")

    def test_one_shot_behavior_with_custom_action(self) -> None:
        """Test 4: One-shot execution triggers once and self-disables."""
        injector = FaultInjector()
        triggered_count = 0

        def crash_action(pt: FaultPoint) -> None:
            nonlocal triggered_count
            triggered_count += 1

        injector.set_crash_action(crash_action)
        injector.enable(FaultPoint.DURING_TRANSACTION_COMMIT, one_shot=True)

        # First trigger matches
        assert injector.trigger(FaultPoint.DURING_TRANSACTION_COMMIT) is True
        assert triggered_count == 1
        assert not injector.is_enabled  # Automatically disabled

        # Second trigger should not fire
        assert injector.trigger(FaultPoint.DURING_TRANSACTION_COMMIT) is False
        assert triggered_count == 1

    def test_unrelated_fault_point_does_not_trigger(self) -> None:
        """Test 5: Fault points that do not match the configured point do not trigger."""
        injector = FaultInjector()
        triggered_count = 0

        def crash_action(pt: FaultPoint) -> None:
            nonlocal triggered_count
            triggered_count += 1

        injector.set_crash_action(crash_action)
        injector.enable(FaultPoint.CHECKPOINT_BEGIN)

        # Unrelated fault point
        assert injector.trigger(FaultPoint.CHECKPOINT_END) is False
        assert triggered_count == 0
        assert injector.is_enabled  # Still waiting for CHECKPOINT_BEGIN

        # Matching fault point
        assert injector.trigger(FaultPoint.CHECKPOINT_BEGIN) is True
        assert triggered_count == 1

    def test_fault_scope_context_manager(self) -> None:
        """Test 6: fault_scope context manager activates and safely cleans up."""
        injector = FaultInjector()
        assert not injector.is_enabled

        with fault_scope(FaultPoint.DURING_TRANSACTION_ABORT, injector=injector) as sc:
            assert sc.is_enabled
            assert sc.configured_fault_point == FaultPoint.DURING_TRANSACTION_ABORT

        assert not injector.is_enabled


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


class TestSubprocessRealCrashes:
    """Tests executing real process terminations across all normal-operation boundaries."""

    def test_normal_operation_disabled_by_default(self, tmp_path: Path) -> None:
        """Verify normal database operations complete with exit code 0 when fault injection is disabled."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.transaction.transaction_manager import TransactionManager
from app.buffer.buffer_manager import BufferManager
from app.checkpoint.checkpoint_manager import CheckpointManager

db_path = Path("test.db")
wal_path = Path("test.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, pool_size=5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    cm = CheckpointManager(lm, tm, bm)

    bm.allocate_page()
    t1 = tm.begin_transaction()
    lsn = lm.append_update(t1.transaction_id, 0, 0, b"old", b"new")
    bm.set_page_lsn(0, lsn)
    bm.flush_page(0)
    tm.commit_transaction(t1.transaction_id)
    cm.create_checkpoint()

    cm.close()
    bm.close()
    tm.close()
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == 0, f"Process failed unexpectedly: {proc.stderr}"

    def test_crash_after_log_append_before_wal_flush(self, tmp_path: Path) -> None:
        """Verify actual process crash at AFTER_LOG_APPEND_BEFORE_WAL_FLUSH."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector

fault_injector.enable(FaultPoint.AFTER_LOG_APPEND_BEFORE_WAL_FLUSH, exit_code={DEFAULT_CRASH_EXIT_CODE})

db_path = Path("test.db")
wal_path = Path("test.wal")

with DiskManager(db_path) as dm, LogManager(wal_path, auto_flush=False) as lm:
    # Append update log record, which triggers crash before any WAL flush
    lm.append_update(
        transaction_id=1,
        page_id=0,
        offset=0,
        before_image=b"before_data",
        after_image=b"after_data",
    )
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        # Verify process terminated with crash exit code
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        # Parent process inspects persistent state after crash
        wal_file = tmp_path / "test.wal"
        assert wal_file.exists()

        # Reopen storage components in parent process
        with LogManager(wal_file) as lm:
            records = lm.read_all()
            assert len(records) == 1
            assert records[0].transaction_id == 1
            assert records[0].before_image == b"before_data"
            assert records[0].after_image == b"after_data"

    def test_crash_after_wal_flush_before_page_flush(self, tmp_path: Path) -> None:
        """Verify crash at AFTER_WAL_FLUSH_BEFORE_PAGE_FLUSH (WAL durable, page not yet persisted)."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.buffer.buffer_manager import BufferManager
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector

db_path = Path("test.db")
wal_path = Path("test.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, pool_size=5, log_manager=lm)
    p0 = bm.allocate_page()
    p0.write(0, b"MODIFIED_BYTES")

    lsn = lm.append_update(
        transaction_id=1,
        page_id=0,
        offset=0,
        before_image=b"\\x00" * 14,
        after_image=b"MODIFIED_BYTES",
    )
    bm.set_page_lsn(0, lsn)

    # Enable crash injection before flush
    fault_injector.enable(FaultPoint.AFTER_WAL_FLUSH_BEFORE_PAGE_FLUSH, exit_code={DEFAULT_CRASH_EXIT_CODE})
    
    # flush_page ensures WAL is durable up to lsn, then triggers crash before disk write!
    bm.flush_page(0)
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        # Parent process verifies persistent state
        db_file = tmp_path / "test.db"
        wal_file = tmp_path / "test.wal"

        assert db_file.exists()
        assert wal_file.exists()

        # WAL was flushed before crash
        with LogManager(wal_file) as lm:
            records = lm.read_all()
            assert len(records) == 1
            assert records[0].after_image == b"MODIFIED_BYTES"

        # Database disk page on physical disk was NOT written (crash occurred before write)
        with DiskManager(db_file) as dm:
            persisted_page = dm.read_page(0)
            raw_bytes = persisted_page.read(0, 14)
            # Must still be initial zero bytes on disk, not MODIFIED_BYTES!
            assert raw_bytes == b"\x00" * 14

    def test_crash_during_transaction_commit(self, tmp_path: Path) -> None:
        """Verify crash at DURING_TRANSACTION_COMMIT."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.transaction.transaction_manager import TransactionManager
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector

fault_injector.enable(FaultPoint.DURING_TRANSACTION_COMMIT, exit_code={DEFAULT_CRASH_EXIT_CODE})

db_path = Path("test.db")
wal_path = Path("test.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    tm = TransactionManager(log_manager=lm)
    t1 = tm.begin_transaction()
    tm.commit_transaction(t1.transaction_id)
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        # Parent verifies WAL contains COMMIT record
        wal_file = tmp_path / "test.wal"
        with LogManager(wal_file) as lm:
            records = lm.read_all()
            assert len(records) == 2  # BEGIN + COMMIT
            assert records[1].record_type.value == "COMMIT"

    def test_crash_during_transaction_abort(self, tmp_path: Path) -> None:
        """Verify crash at DURING_TRANSACTION_ABORT."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.transaction.transaction_manager import TransactionManager
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector

fault_injector.enable(FaultPoint.DURING_TRANSACTION_ABORT, exit_code={DEFAULT_CRASH_EXIT_CODE})

db_path = Path("test.db")
wal_path = Path("test.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    tm = TransactionManager(log_manager=lm)
    t1 = tm.begin_transaction()
    tm.abort_transaction(t1.transaction_id)
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        # Parent verifies WAL contains ABORT record
        wal_file = tmp_path / "test.wal"
        with LogManager(wal_file) as lm:
            records = lm.read_all()
            assert len(records) == 2  # BEGIN + ABORT
            assert records[1].record_type.value == "ABORT"

    def test_crash_at_checkpoint_begin(self, tmp_path: Path) -> None:
        """Verify crash at CHECKPOINT_BEGIN leaves incomplete checkpoint."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.transaction.transaction_manager import TransactionManager
from app.buffer.buffer_manager import BufferManager
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector

fault_injector.enable(FaultPoint.CHECKPOINT_BEGIN, exit_code={DEFAULT_CRASH_EXIT_CODE})

db_path = Path("test.db")
wal_path = Path("test.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, pool_size=5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    cm = CheckpointManager(lm, tm, bm)

    cm.create_checkpoint()
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        # Parent inspects WAL: CHECKPOINT_BEGIN exists but no CHECKPOINT_END
        wal_file = tmp_path / "test.wal"
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            cm = CheckpointManager(lm, tm, bm)

            records = lm.read_all()
            assert len(records) == 1
            assert records[0].record_type.value == "CHECKPOINT_BEGIN"

            # Incomplete checkpoint is ignored
            assert cm.get_latest_checkpoint() is None

    def test_crash_at_checkpoint_end(self, tmp_path: Path) -> None:
        """Verify crash at CHECKPOINT_END produces durable completed checkpoint."""
        script = f"""
from pathlib import Path
from app.storage.disk_manager import DiskManager
from app.wal.log_manager import LogManager
from app.transaction.transaction_manager import TransactionManager
from app.buffer.buffer_manager import BufferManager
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector

fault_injector.enable(FaultPoint.CHECKPOINT_END, exit_code={DEFAULT_CRASH_EXIT_CODE})

db_path = Path("test.db")
wal_path = Path("test.wal")

with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
    bm = BufferManager(dm, pool_size=5, log_manager=lm)
    tm = TransactionManager(log_manager=lm)
    cm = CheckpointManager(lm, tm, bm)

    cm.create_checkpoint()
"""
        proc = _run_subprocess_snippet(script, tmp_path)
        assert proc.returncode == DEFAULT_CRASH_EXIT_CODE

        # Parent verifies completed checkpoint exists
        wal_file = tmp_path / "test.wal"
        db_file = tmp_path / "test.db"
        with DiskManager(db_file) as dm, LogManager(wal_file) as lm:
            bm = BufferManager(dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            cm = CheckpointManager(lm, tm, bm)

            latest = cm.get_latest_checkpoint()
            assert latest is not None
            assert latest.begin_lsn == 1
            assert latest.end_lsn == 2
