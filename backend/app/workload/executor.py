"""Execution engine for synthetic transaction workloads with recovery and crash validation."""

import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional
from app.buffer.buffer_manager import BufferManager
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.fault.constants import DEFAULT_CRASH_EXIT_CODE, FaultPoint
from app.fault.fault_injector import fault_injector
from app.recovery.recovery_manager import RecoveryManager
from app.recovery.recovery_state import RecoveryResult
from app.storage.constants import PAGE_SIZE
from app.storage.disk_manager import DiskManager
from app.transaction.transaction_manager import TransactionManager
from app.wal.log_manager import LogManager
from app.wal.log_record import LogRecordType
from app.workload.config import WorkloadConfig
from app.workload.exceptions import WorkloadExecutionError, WorkloadValidationError
from app.workload.generator import WorkloadGenerator
from app.workload.models import (
    OperationType,
    TransactionOutcome,
    WorkloadOperation,
    WorkloadPlan,
    WorkloadResult,
)


class WorkloadExecutor:
    """Executes generated workload plans on the ARIES database engine and validates outcomes."""

    @classmethod
    def execute(
        cls,
        config: WorkloadConfig,
        plan: Optional[WorkloadPlan] = None,
        db_path: Optional[Path] = None,
        wal_path: Optional[Path] = None,
        base_init: bool = True,
    ) -> WorkloadResult:
        """Execute a workload against the persistent database components.

        Args:
            config: WorkloadConfig specifying parameters.
            plan: Optional pre-generated WorkloadPlan. If None, generated from config.
            db_path: Path to database binary file.
            wal_path: Path to WAL binary file.
            base_init: If True, initializes newly allocated pages with baseline patterns.

        Returns:
            WorkloadResult containing execution counts, transaction states, and validation.

        Raises:
            WorkloadExecutionError: If files or parameters are invalid.
        """
        if db_path is None or wal_path is None:
            raise WorkloadExecutionError("db_path and wal_path must both be provided.")

        db_path = Path(db_path)
        wal_path = Path(wal_path)

        if plan is None:
            plan = WorkloadGenerator.generate(config)

        # 1. Initialize subsystem managers
        with DiskManager(db_path=db_path) as dm, LogManager(wal_path=wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=config.pool_size, log_manager=lm)
            tm = TransactionManager(log_manager=lm)
            cm = (
                CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)
                if config.checkpoint_interval is not None
                else None
            )

            # 2. Allocate and initialize accessible pages
            allocated_count = dm.get_page_count()
            while allocated_count < config.num_pages:
                new_p = bm.allocate_page()
                page_id = new_p.page_id
                if base_init:
                    init_pat = f"PAGE_{page_id:04d}_INIT_BASE_DATA__".encode("ascii")
                    full_init = (init_pat * (PAGE_SIZE // len(init_pat) + 1))[:PAGE_SIZE]
                    new_p.write(0, full_init)
                bm.unpin_page(page_id, is_dirty=True)
                allocated_count = dm.get_page_count()

            bm.flush_all_pages()

            # Record initial baseline page contents for expected state tracking
            expected_committed_pages: Dict[int, bytearray] = {}
            for p_idx in range(config.num_pages):
                page = bm.fetch_page(p_idx)
                expected_committed_pages[p_idx] = bytearray(page.read(0, PAGE_SIZE))
                bm.unpin_page(p_idx, is_dirty=False)

            # 3. Execution tracking state
            executed_operations = 0
            reads_executed = 0
            updates_executed = 0
            transactions_started: List[int] = []
            transactions_committed: List[int] = []
            transactions_aborted: List[int] = []
            tx_updates: Dict[int, List[Dict[str, Any]]] = {}

            # 4. Execute operation plan
            for op in plan.operations:
                if op.operation_type == OperationType.BEGIN:
                    tx = tm.begin_transaction()
                    transactions_started.append(tx.transaction_id)
                    executed_operations += 1

                elif op.operation_type == OperationType.READ:
                    assert op.page_id is not None
                    assert op.offset is not None
                    assert op.length is not None
                    page = bm.fetch_page(op.page_id)
                    _ = page.read(op.offset, op.length)
                    bm.unpin_page(op.page_id, is_dirty=False)
                    reads_executed += 1
                    executed_operations += 1

                elif op.operation_type == OperationType.UPDATE:
                    assert op.page_id is not None
                    assert op.offset is not None
                    assert op.length is not None
                    assert op.data is not None
                    page = bm.fetch_page(op.page_id)
                    before_image = page.read(op.offset, op.length)
                    after_image = op.data
                    lsn = lm.append_update(
                        op.transaction_id,
                        op.page_id,
                        op.offset,
                        before_image,
                        after_image,
                    )
                    page.write(op.offset, after_image)
                    bm.set_page_lsn(op.page_id, lsn)
                    bm.unpin_page(op.page_id, is_dirty=True)

                    tx_updates.setdefault(op.transaction_id, []).append(
                        {
                            "page_id": op.page_id,
                            "offset": op.offset,
                            "length": op.length,
                            "before_image": before_image,
                            "after_image": after_image,
                            "lsn": lsn,
                        }
                    )
                    updates_executed += 1
                    executed_operations += 1

                elif op.operation_type == OperationType.COMMIT:
                    tm.commit_transaction(op.transaction_id)
                    lm.flush()
                    transactions_committed.append(op.transaction_id)
                    executed_operations += 1

                    # Apply committed modifications to expected model
                    for upd in tx_updates.get(op.transaction_id, []):
                        pid = upd["page_id"]
                        off = upd["offset"]
                        aimg = upd["after_image"]
                        expected_committed_pages[pid][off : off + len(aimg)] = aimg

                elif op.operation_type == OperationType.ABORT:
                    # In ARIES, runtime rollback compensates updates in reverse LSN order
                    for upd in reversed(tx_updates.get(op.transaction_id, [])):
                        pid = upd["page_id"]
                        off = upd["offset"]
                        bimg = upd["before_image"]
                        rec = lm.get_log_record(upd["lsn"])
                        undo_next = rec.prev_lsn if rec is not None else None
                        clr_lsn = lm.append_clr(
                            op.transaction_id,
                            pid,
                            off,
                            undo_next_lsn=undo_next,
                            compensation_image=bimg,
                        )
                        page = bm.fetch_page(pid)
                        page.write(off, bimg)
                        bm.set_page_lsn(pid, clr_lsn)
                        bm.unpin_page(pid, is_dirty=True)

                    tm.abort_transaction(op.transaction_id)
                    lm.flush()
                    transactions_aborted.append(op.transaction_id)
                    executed_operations += 1

                # Checkpoint interval check
                if cm is not None and config.checkpoint_interval is not None:
                    if executed_operations % config.checkpoint_interval == 0:
                        cm.create_checkpoint()

            # 5. Determine incomplete transactions
            transactions_incomplete = [
                tx_id
                for tx_id in transactions_started
                if tx_id not in transactions_committed and tx_id not in transactions_aborted
            ]

            # 6. Check pin counts before closing
            validation_errors: List[str] = []
            for pid in bm.get_resident_pages():
                pins = bm.get_pin_count(pid)
                if pins != 0:
                    validation_errors.append(f"Page {pid} leaked frame pin: pin_count={pins}")

            # 7. Close buffer manager according to execution type
            if transactions_incomplete or config.crash_fault_point is not None:
                # Simulate crash/abrupt shutdown: do not flush unflushed dirty pages
                bm.close(flush_dirty=False)
                tm.close()
                lm.close()
                dm.close()
            else:
                # Clean shutdown: flush all dirty pages
                bm.flush_all_pages()
                bm.close(flush_dirty=True)
                tm.close()
                lm.close()
                dm.close()

        # 8. Post-workload Recovery (if incomplete transactions exist and auto_recover=True)
        recovery_result: Optional[RecoveryResult] = None
        if (transactions_incomplete or config.crash_fault_point is not None) and config.auto_recover:
            recovery_result = RecoveryManager.recover_from_files(
                db_path=db_path,
                wal_path=wal_path,
                pool_size=config.pool_size,
            )

        # 9. Independent state and recovery verification
        validation_errors.extend(
            cls.validate_database_state(
                db_path=db_path,
                expected_committed_pages=expected_committed_pages,
                recovery_result=recovery_result,
                expected_winners=transactions_committed,
                expected_losers=transactions_incomplete,
            )
        )

        is_valid = (len(validation_errors) == 0)

        return WorkloadResult(
            config=config,
            seed=plan.config.seed,
            planned_operations=len(plan),
            executed_operations=executed_operations,
            transactions_started=len(transactions_started),
            transactions_committed=len(transactions_committed),
            transactions_aborted=len(transactions_aborted),
            transactions_incomplete=len(transactions_incomplete),
            reads_executed=reads_executed,
            updates_executed=updates_executed,
            crash_configured=config.crash_fault_point is not None,
            crash_observed=False,
            fault_point=config.crash_fault_point.value if config.crash_fault_point else None,
            exit_code=None,
            recovery_result=recovery_result,
            is_valid=is_valid,
            validation_errors=validation_errors,
            committed_transactions=transactions_committed,
            aborted_transactions=transactions_aborted,
            incomplete_transactions=transactions_incomplete,
        )

    @classmethod
    def validate_database_state(
        cls,
        db_path: Path,
        expected_committed_pages: Dict[int, bytearray],
        recovery_result: Optional[RecoveryResult] = None,
        expected_winners: Optional[List[int]] = None,
        expected_losers: Optional[List[int]] = None,
    ) -> List[str]:
        """Validate persistent disk page contents and recovery outcome against expected model.

        Args:
            db_path: Database filepath to read from disk.
            expected_committed_pages: Mapping from page ID to expected bytearray content.
            recovery_result: Optional RecoveryResult to inspect.
            expected_winners: Optional expected list of winner transaction IDs.
            expected_losers: Optional expected list of loser transaction IDs.

        Returns:
            List of validation error strings (empty if valid).
        """
        validation_errors: List[str] = []

        # 1. Persistent on-disk verification
        with DiskManager(db_path=db_path) as dm:
            page_count = dm.get_page_count()
            for pid, expected_bytes in expected_committed_pages.items():
                if pid >= page_count:
                    validation_errors.append(
                        f"Page {pid} expected but not allocated on disk (allocated: {page_count})."
                    )
                    continue
                disk_page = dm.read_page(pid)
                actual_bytes = disk_page.read(0, PAGE_SIZE)
                if actual_bytes != bytes(expected_bytes):
                    validation_errors.append(
                        f"Page {pid} persistent disk state mismatch against expected committed model."
                    )

        # 2. Recovery result verification
        if recovery_result is not None:
            if not recovery_result.is_success:
                validation_errors.append(f"Recovery failed with error: {recovery_result.error_message}")
            if expected_winners is not None and set(recovery_result.winners) != set(expected_winners):
                validation_errors.append(
                    f"Recovery winners mismatch: expected {expected_winners}, got {recovery_result.winners}"
                )
            if expected_losers is not None and set(recovery_result.losers) != set(expected_losers):
                validation_errors.append(
                    f"Recovery losers mismatch: expected {expected_losers}, got {recovery_result.losers}"
                )

        return validation_errors

    @classmethod
    def run_crash_experiment(
        cls,
        config: WorkloadConfig,
        db_path: Path,
        wal_path: Path,
        temp_dir: Path,
        timeout: float = 30.0,
    ) -> WorkloadResult:
        """Run a crash experiment in an isolated child process and recover in the parent process.

        Args:
            config: WorkloadConfig with crash_fault_point configured.
            db_path: Persistent database filepath.
            wal_path: Persistent WAL filepath.
            temp_dir: Directory for execution context.
            timeout: Subprocess execution timeout in seconds (default 30s).

        Returns:
            WorkloadResult describing the crash, recovery, and persistent verification outcome.

        Raises:
            WorkloadExecutionError: If crash_fault_point is not configured or execution fails.
        """
        if config.crash_fault_point is None:
            raise WorkloadExecutionError("run_crash_experiment requires config.crash_fault_point to be set.")

        fault_point_name = config.crash_fault_point.value
        exit_code = config.crash_exit_code
        plan = WorkloadGenerator.generate(config)

        # Subprocess script: enables fault injector and runs workload
        script = f"""
from pathlib import Path
from app.fault.constants import FaultPoint
from app.fault.fault_injector import fault_injector
from app.workload.config import WorkloadConfig
from app.workload.executor import WorkloadExecutor

db_path = Path({str(db_path)!r})
wal_path = Path({str(wal_path)!r})

config = WorkloadConfig(
    num_transactions={config.num_transactions},
    num_pages={config.num_pages},
    operations_per_transaction={config.operations_per_transaction},
    read_ratio={config.read_ratio},
    commit_ratio={config.commit_ratio},
    abort_ratio={config.abort_ratio},
    incomplete_ratio={config.incomplete_ratio},
    same_page_probability={config.same_page_probability},
    interleave_probability={config.interleave_probability},
    seed={config.seed},
    pool_size={config.pool_size},
    crash_fault_point={fault_point_name!r},
    crash_exit_code={exit_code},
    auto_recover=False,
)

fault_injector.enable(FaultPoint({fault_point_name!r}), exit_code={exit_code})
WorkloadExecutor.execute(config=config, db_path=db_path, wal_path=wal_path)
"""
        backend_dir = str(Path(__file__).resolve().parent.parent.parent)
        env = dict(os.environ)
        env["PYTHONPATH"] = backend_dir

        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(temp_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkloadExecutionError(f"Crash experiment subprocess timed out after {timeout}s: {exc}") from exc

        crash_observed = (proc.returncode == exit_code)
        validation_errors: List[str] = []

        if not crash_observed:
            validation_errors.append(
                f"Expected crash with exit code {exit_code}, but subprocess exited with code {proc.returncode}: {proc.stderr}"
            )

        # In parent process, perform restart recovery using the exact same files
        recovery_result: Optional[RecoveryResult] = None
        if config.auto_recover and db_path.exists() and wal_path.exists():
            recovery_result = RecoveryManager.recover_from_files(
                db_path=db_path,
                wal_path=wal_path,
                pool_size=config.pool_size,
            )
            if not recovery_result.is_success:
                validation_errors.append(f"Recovery failed: {recovery_result.error_message}")

        # Independent expected-state validation
        expected_pages: Dict[int, bytearray] = {}
        for pid in range(config.num_pages):
            pat = f"PAGE_{pid:04d}_INIT_BASE_DATA__".encode("ascii")
            expected_pages[pid] = bytearray((pat * (PAGE_SIZE // len(pat) + 1))[:PAGE_SIZE])

        winners = recovery_result.winners if recovery_result else []
        losers = recovery_result.losers if recovery_result else []

        for op in plan.operations:
            if op.operation_type == OperationType.UPDATE and op.transaction_id in winners:
                assert op.page_id is not None
                assert op.offset is not None
                assert op.data is not None
                expected_pages[op.page_id][op.offset : op.offset + len(op.data)] = op.data

        if db_path.exists():
            validation_errors.extend(
                cls.validate_database_state(
                    db_path=db_path,
                    expected_committed_pages=expected_pages,
                    recovery_result=recovery_result,
                    expected_winners=winners,
                    expected_losers=losers,
                )
            )

        # Inspect WAL to record executed operations up to crash
        executed_ops = 0
        updates_executed = 0
        if wal_path.exists():
            try:
                with LogManager(wal_path=wal_path) as lm:
                    wal_recs = lm.read_all()
                    executed_ops = len(wal_recs)
                    updates_executed = sum(1 for r in wal_recs if r.record_type == LogRecordType.UPDATE)
            except Exception:
                pass

        is_valid = (len(validation_errors) == 0)

        return WorkloadResult(
            config=config,
            seed=config.seed,
            planned_operations=len(plan),
            executed_operations=executed_ops,
            transactions_started=config.num_transactions,
            transactions_committed=len(winners),
            transactions_aborted=0,
            transactions_incomplete=len(losers),
            reads_executed=0,
            updates_executed=updates_executed,
            crash_configured=True,
            crash_observed=crash_observed,
            fault_point=fault_point_name,
            exit_code=proc.returncode,
            recovery_result=recovery_result,
            is_valid=is_valid,
            validation_errors=validation_errors,
            committed_transactions=winners,
            aborted_transactions=[],
            incomplete_transactions=losers,
        )
