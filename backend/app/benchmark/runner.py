"""Execution engine for running benchmark scenarios and collecting performance metrics."""

from collections import deque
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, List, Optional
from app.benchmark.config import BenchmarkConfig
from app.benchmark.exceptions import BenchmarkExecutionError
from app.benchmark.models import (
    BenchmarkRunResult,
    BenchmarkScenarioResult,
    EnvironmentMetadata,
    RecoveryRunMetrics,
    WorkloadRunMetrics,
)
from app.buffer.buffer_manager import BufferManager
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.recovery.analysis import Analysis
from app.recovery.recovery_manager import RecoveryManager
from app.recovery.recovery_state import RecoveryResult
from app.recovery.redo import Redo
from app.recovery.undo import Undo
from app.storage.constants import PAGE_SIZE
from app.storage.disk_manager import DiskManager
from app.transaction.transaction_manager import TransactionManager
from app.wal.log_manager import LogManager
from app.workload.config import WorkloadConfig
from app.workload.executor import WorkloadExecutor
from app.workload.generator import WorkloadGenerator
from app.workload.models import OperationType, WorkloadPlan


class BenchmarkRunner:
    """Executes benchmark scenarios with isolated environments and timing measurements."""

    @classmethod
    def run(
        cls,
        config: BenchmarkConfig,
        target_dir: Optional[Path] = None,
    ) -> BenchmarkScenarioResult:
        """Run a complete benchmark scenario including warmup and measured repetitions.

        Args:
            config: BenchmarkConfig specifying scenario parameters.
            target_dir: Optional base directory for temporary run files.

        Returns:
            BenchmarkScenarioResult with per-run and aggregate statistics.
        """
        env: Optional[EnvironmentMetadata] = None
        if config.collect_environment_metadata:
            env = EnvironmentMetadata.capture()

        all_runs: List[BenchmarkRunResult] = []
        total_runs = config.warmup_runs + config.repetitions

        for run_idx in range(total_runs):
            is_warmup = run_idx < config.warmup_runs
            run_result = cls._execute_single_run(
                config=config,
                run_index=run_idx,
                is_warmup=is_warmup,
                target_dir=target_dir,
            )
            all_runs.append(run_result)

        return BenchmarkScenarioResult(
            config=config,
            runs=all_runs,
            environment=env,
        )

    @classmethod
    def _execute_single_run(
        cls,
        config: BenchmarkConfig,
        run_index: int,
        is_warmup: bool,
        target_dir: Optional[Path],
    ) -> BenchmarkRunResult:
        if target_dir is not None:
            Path(target_dir).mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=str(target_dir) if target_dir else None) as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            db_path = temp_dir / f"run_{run_index:03d}.db"
            wal_path = temp_dir / f"run_{run_index:03d}.wal"

            try:
                if config.workload_config.crash_fault_point is not None:
                    return cls._execute_crash_run(
                        config=config,
                        run_index=run_index,
                        is_warmup=is_warmup,
                        db_path=db_path,
                        wal_path=wal_path,
                        temp_dir=temp_dir,
                    )
                elif config.is_recovery_only:
                    return cls._execute_recovery_only_run(
                        config=config,
                        run_index=run_index,
                        is_warmup=is_warmup,
                        db_path=db_path,
                        wal_path=wal_path,
                        temp_dir=temp_dir,
                    )
                else:
                    return cls._execute_workload_run(
                        config=config,
                        run_index=run_index,
                        is_warmup=is_warmup,
                        db_path=db_path,
                        wal_path=wal_path,
                    )
            except Exception as exc:
                return BenchmarkRunResult(
                    run_index=run_index,
                    is_warmup=is_warmup,
                    is_success=False,
                    validation_passed=False,
                    validation_errors=[f"Run failed with exception: {exc}"],
                    error_message=str(exc),
                    workload_metrics=None,
                    recovery_metrics=None,
                )

    # -------------------------------------------------------------------------
    # Workload Run Execution
    # -------------------------------------------------------------------------
    @classmethod
    def _execute_workload_run(
        cls,
        config: BenchmarkConfig,
        run_index: int,
        is_warmup: bool,
        db_path: Path,
        wal_path: Path,
    ) -> BenchmarkRunResult:
        """Execute a standard workload benchmark run with separated setup, workload, and recovery timing."""
        w_cfg = config.workload_config
        plan = WorkloadGenerator.generate(w_cfg, seed=config.seed)

        # 1. Setup Phase (OUTSIDE timed workload interval): Page initialization
        with DiskManager(db_path=db_path) as dm, LogManager(wal_path=wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=w_cfg.pool_size, log_manager=lm)
            for pid in range(w_cfg.num_pages):
                p = bm.allocate_page()
                init_pat = f"PAGE_{pid:04d}_INIT_BASE_DATA__".encode("ascii")
                full_init = (init_pat * (PAGE_SIZE // len(init_pat) + 1))[:PAGE_SIZE]
                p.write(0, full_init)
                bm.unpin_page(pid, is_dirty=True)
            bm.flush_all_pages()

            # Record initial baseline page contents
            expected_committed_pages: Dict[int, bytearray] = {}
            for p_idx in range(w_cfg.num_pages):
                page = bm.fetch_page(p_idx)
                expected_committed_pages[p_idx] = bytearray(page.read(0, PAGE_SIZE))
                bm.unpin_page(p_idx, is_dirty=False)

            tm = TransactionManager(log_manager=lm)
            cm = (
                CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)
                if w_cfg.checkpoint_interval is not None
                else None
            )

            executed_operations = 0
            reads_executed = 0
            updates_executed = 0
            transactions_started: List[int] = []
            transactions_committed: List[int] = []
            transactions_aborted: List[int] = []
            tx_updates: Dict[int, List[Dict[str, Any]]] = {}

            # 2. Timed Workload Execution Interval
            t_workload_start = time.perf_counter()

            for op in plan.operations:
                if op.operation_type == OperationType.BEGIN:
                    tx = tm.begin_transaction()
                    transactions_started.append(tx.transaction_id)
                    executed_operations += 1

                elif op.operation_type == OperationType.READ:
                    assert op.page_id is not None and op.offset is not None and op.length is not None
                    page = bm.fetch_page(op.page_id)
                    _ = page.read(op.offset, op.length)
                    bm.unpin_page(op.page_id, is_dirty=False)
                    reads_executed += 1
                    executed_operations += 1

                elif op.operation_type == OperationType.UPDATE:
                    assert op.page_id is not None and op.offset is not None and op.length is not None and op.data is not None
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

                    for upd in tx_updates.get(op.transaction_id, []):
                        pid = upd["page_id"]
                        off = upd["offset"]
                        aimg = upd["after_image"]
                        expected_committed_pages[pid][off : off + len(aimg)] = aimg

                elif op.operation_type == OperationType.ABORT:
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

                if cm is not None and w_cfg.checkpoint_interval is not None:
                    if executed_operations % w_cfg.checkpoint_interval == 0:
                        cm.create_checkpoint()

            t_workload_end = time.perf_counter()
            workload_duration_s = t_workload_end - t_workload_start

            transactions_incomplete = [
                tx_id
                for tx_id in transactions_started
                if tx_id not in transactions_committed and tx_id not in transactions_aborted
            ]

            # Check pin safety
            validation_errors: List[str] = []
            for pid in bm.get_resident_pages():
                pins = bm.get_pin_count(pid)
                if pins != 0:
                    validation_errors.append(f"Page {pid} leaked frame pin: pin_count={pins}")

            # Close buffer manager
            if transactions_incomplete:
                bm.close(flush_dirty=False)
            else:
                bm.flush_all_pages()
                bm.close(flush_dirty=True)
            tm.close()

        # 3. Timed Recovery Interval (if incomplete transactions exist and auto_recover is enabled)
        recovery_result: Optional[RecoveryResult] = None
        recovery_metrics: Optional[RecoveryRunMetrics] = None

        if transactions_incomplete and w_cfg.auto_recover:
            rec_result, rec_metrics = cls._run_timed_recovery(
                db_path=db_path,
                wal_path=wal_path,
                pool_size=w_cfg.pool_size,
                measure_stages=config.measure_recovery_stages,
            )
            recovery_result = rec_result
            recovery_metrics = rec_metrics

        # 4. Independent Validation
        val_errors = WorkloadExecutor.validate_database_state(
            db_path=db_path,
            expected_committed_pages=expected_committed_pages,
            recovery_result=recovery_result,
            expected_winners=transactions_committed,
            expected_losers=transactions_incomplete if w_cfg.auto_recover else [],
        )
        validation_errors.extend(val_errors)

        is_valid = (len(validation_errors) == 0)

        workload_metrics = WorkloadRunMetrics.create(
            duration_s=workload_duration_s,
            transactions_started=len(transactions_started),
            transactions_committed=len(transactions_committed),
            transactions_aborted=len(transactions_aborted),
            transactions_incomplete=len(transactions_incomplete),
            reads_executed=reads_executed,
            updates_executed=updates_executed,
            executed_operations=executed_operations,
        )

        return BenchmarkRunResult(
            run_index=run_index,
            is_warmup=is_warmup,
            is_success=is_valid,
            validation_passed=is_valid,
            validation_errors=validation_errors,
            error_message="; ".join(validation_errors) if validation_errors else None,
            workload_metrics=workload_metrics,
            recovery_metrics=recovery_metrics,
        )

    # -------------------------------------------------------------------------
    # Recovery-Only Run Execution
    # -------------------------------------------------------------------------
    @classmethod
    def _execute_recovery_only_run(
        cls,
        config: BenchmarkConfig,
        run_index: int,
        is_warmup: bool,
        db_path: Path,
        wal_path: Path,
        temp_dir: Path,
    ) -> BenchmarkRunResult:
        """Execute a recovery-only benchmark run where crash state is prepared outside timing."""
        w_cfg = config.workload_config

        # 1. Setup Phase (OUTSIDE timed recovery interval): prepare database and WAL
        prep_config = WorkloadConfig(
            num_transactions=w_cfg.num_transactions,
            num_pages=w_cfg.num_pages,
            operations_per_transaction=w_cfg.operations_per_transaction,
            read_ratio=w_cfg.read_ratio,
            commit_ratio=w_cfg.commit_ratio,
            abort_ratio=w_cfg.abort_ratio,
            incomplete_ratio=w_cfg.incomplete_ratio,
            same_page_probability=w_cfg.same_page_probability,
            interleave_probability=w_cfg.interleave_probability,
            seed=config.seed,
            pool_size=w_cfg.pool_size,
            auto_recover=False,  # Leave database in crash state
        )

        prep_result = WorkloadExecutor.execute(
            config=prep_config,
            db_path=db_path,
            wal_path=wal_path,
        )

        # 2. Timed Recovery Interval
        rec_result, rec_metrics = cls._run_timed_recovery(
            db_path=db_path,
            wal_path=wal_path,
            pool_size=w_cfg.pool_size,
            measure_stages=config.measure_recovery_stages,
        )

        # 3. Post-Recovery Validation
        expected_pages: Dict[int, bytearray] = {}
        for pid in range(w_cfg.num_pages):
            pat = f"PAGE_{pid:04d}_INIT_BASE_DATA__".encode("ascii")
            expected_pages[pid] = bytearray((pat * (PAGE_SIZE // len(pat) + 1))[:PAGE_SIZE])

        plan = WorkloadGenerator.generate(prep_config, seed=config.seed)
        for op in plan.operations:
            if op.operation_type == OperationType.UPDATE and op.transaction_id in rec_result.winners:
                assert op.page_id is not None and op.offset is not None and op.data is not None
                expected_pages[op.page_id][op.offset : op.offset + len(op.data)] = op.data

        validation_errors = WorkloadExecutor.validate_database_state(
            db_path=db_path,
            expected_committed_pages=expected_pages,
            recovery_result=rec_result,
            expected_winners=rec_result.winners,
            expected_losers=rec_result.losers,
        )

        is_valid = (len(validation_errors) == 0 and rec_result.is_success)

        return BenchmarkRunResult(
            run_index=run_index,
            is_warmup=is_warmup,
            is_success=is_valid,
            validation_passed=is_valid,
            validation_errors=validation_errors,
            error_message="; ".join(validation_errors) if validation_errors else None,
            workload_metrics=None,  # Recovery-only scenario does not time workload
            recovery_metrics=rec_metrics,
        )

    # -------------------------------------------------------------------------
    # Crash Experiment Run Execution
    # -------------------------------------------------------------------------
    @classmethod
    def _execute_crash_run(
        cls,
        config: BenchmarkConfig,
        run_index: int,
        is_warmup: bool,
        db_path: Path,
        wal_path: Path,
        temp_dir: Path,
    ) -> BenchmarkRunResult:
        """Execute a crash experiment benchmark run with separated crash and recovery timing."""
        w_cfg = config.workload_config

        # 1. Run crash experiment in child process without automatic recovery in parent
        crash_prep_config = WorkloadConfig(
            num_transactions=w_cfg.num_transactions,
            num_pages=w_cfg.num_pages,
            operations_per_transaction=w_cfg.operations_per_transaction,
            read_ratio=w_cfg.read_ratio,
            commit_ratio=w_cfg.commit_ratio,
            abort_ratio=w_cfg.abort_ratio,
            incomplete_ratio=w_cfg.incomplete_ratio,
            same_page_probability=w_cfg.same_page_probability,
            interleave_probability=w_cfg.interleave_probability,
            seed=config.seed,
            pool_size=w_cfg.pool_size,
            crash_fault_point=w_cfg.crash_fault_point,
            crash_exit_code=w_cfg.crash_exit_code,
            auto_recover=False,
        )

        t_crash_start = time.perf_counter()
        crash_res = WorkloadExecutor.run_crash_experiment(
            config=crash_prep_config,
            db_path=db_path,
            wal_path=wal_path,
            temp_dir=temp_dir,
            timeout=config.timeout_per_run,
        )
        crash_duration_s = time.perf_counter() - t_crash_start

        validation_errors: List[str] = list(crash_res.validation_errors)
        rec_metrics: Optional[RecoveryRunMetrics] = None
        recovery_result: Optional[RecoveryResult] = None

        # 2. Timed Recovery Interval (measured separately in parent process)
        if crash_res.crash_observed and db_path.exists() and wal_path.exists():
            rec_result, rec_metrics = cls._run_timed_recovery(
                db_path=db_path,
                wal_path=wal_path,
                pool_size=w_cfg.pool_size,
                measure_stages=config.measure_recovery_stages,
            )
            recovery_result = rec_result

            # 3. Post-Recovery Validation
            expected_pages: Dict[int, bytearray] = {}
            for pid in range(w_cfg.num_pages):
                pat = f"PAGE_{pid:04d}_INIT_BASE_DATA__".encode("ascii")
                expected_pages[pid] = bytearray((pat * (PAGE_SIZE // len(pat) + 1))[:PAGE_SIZE])

            plan = WorkloadGenerator.generate(crash_prep_config, seed=config.seed)
            for op in plan.operations:
                if op.operation_type == OperationType.UPDATE and op.transaction_id in rec_result.winners:
                    assert op.page_id is not None and op.offset is not None and op.data is not None
                    expected_pages[op.page_id][op.offset : op.offset + len(op.data)] = op.data

            val_errors = WorkloadExecutor.validate_database_state(
                db_path=db_path,
                expected_committed_pages=expected_pages,
                recovery_result=rec_result,
                expected_winners=rec_result.winners,
                expected_losers=rec_result.losers,
            )
            validation_errors.extend(val_errors)
        else:
            validation_errors.append("Crash fault was not reached or database/WAL files are missing.")

        is_valid = (
            len(validation_errors) == 0
            and crash_res.crash_observed
            and recovery_result is not None
            and recovery_result.is_success
        )

        workload_metrics = WorkloadRunMetrics.create(
            duration_s=crash_duration_s,
            transactions_started=crash_res.transactions_started,
            transactions_committed=len(recovery_result.winners) if recovery_result else 0,
            transactions_aborted=0,
            transactions_incomplete=len(recovery_result.losers) if recovery_result else 0,
            reads_executed=crash_res.reads_executed,
            updates_executed=crash_res.updates_executed,
            executed_operations=crash_res.executed_operations,
        )

        return BenchmarkRunResult(
            run_index=run_index,
            is_warmup=is_warmup,
            is_success=is_valid,
            validation_passed=is_valid,
            validation_errors=validation_errors,
            error_message="; ".join(validation_errors) if validation_errors else None,
            workload_metrics=workload_metrics,
            recovery_metrics=rec_metrics,
        )

    # -------------------------------------------------------------------------
    # Helper: Timed Recovery Execution
    # -------------------------------------------------------------------------
    @classmethod
    def _run_timed_recovery(
        cls,
        db_path: Path,
        wal_path: Path,
        pool_size: int,
        measure_stages: bool,
    ) -> tuple[RecoveryResult, RecoveryRunMetrics]:
        """Execute recovery and capture duration and optional stage metrics."""
        t_rec_start = time.perf_counter()

        if measure_stages:
            with DiskManager(db_path=db_path) as dm, LogManager(wal_path=wal_path) as lm:
                bm = BufferManager(disk_manager=dm, pool_size=pool_size, log_manager=lm)
                try:
                    # Stage 1: Analysis
                    t_a0 = time.perf_counter()
                    analysis = Analysis(log_manager=lm, checkpoint_manager=None)
                    analysis_res = analysis.execute()
                    t_analysis = time.perf_counter() - t_a0

                    # Stage 2: Redo
                    t_r0 = time.perf_counter()
                    redo = Redo(log_manager=lm, buffer_manager=bm)
                    redo_res = redo.execute(analysis_res)
                    t_redo = time.perf_counter() - t_r0

                    # Stage 3: Undo
                    t_u0 = time.perf_counter()
                    undo = Undo(log_manager=lm, buffer_manager=bm)
                    undo_res = undo.execute(analysis_res)
                    t_undo = time.perf_counter() - t_u0

                    # Verification & persistence
                    bm.flush_all_pages()

                    rec_result = RecoveryResult(
                        is_success=True,
                        analysis_result=analysis_res,
                        redo_result=redo_res,
                        undo_result=undo_res,
                        stage="COMPLETE",
                    )
                except Exception as exc:
                    rec_result = RecoveryResult(
                        is_success=False,
                        error_message=str(exc),
                        stage="FAILED",
                    )
                    t_analysis = None
                    t_redo = None
                    t_undo = None
                finally:
                    bm.close(flush_dirty=False)

            total_rec_duration = time.perf_counter() - t_rec_start
        else:
            rec_result = RecoveryManager.recover_from_files(
                db_path=db_path,
                wal_path=wal_path,
                pool_size=pool_size,
            )
            total_rec_duration = time.perf_counter() - t_rec_start
            t_analysis = None
            t_redo = None
            t_undo = None

        rec_metrics = RecoveryRunMetrics.create(
            total_duration_s=total_rec_duration,
            analysis_duration_s=t_analysis,
            redo_duration_s=t_redo,
            undo_duration_s=t_undo,
            records_redone=rec_result.records_redone,
            clrs_generated=rec_result.clrs_generated,
            updates_undone=rec_result.updates_undone,
            pages_modified_count=len(rec_result.pages_modified),
            winners_count=len(rec_result.winners),
            losers_count=len(rec_result.losers),
        )

        return rec_result, rec_metrics
