"""Monitoring state registry and data extraction service for database observability."""

import os
import threading
import time
from typing import Any, Dict, List, Optional
from app.benchmark.models import BenchmarkScenarioResult
from app.buffer.buffer_manager import BufferManager
from app.buffer.exceptions import BufferManagerClosedError, PageNotResidentError
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.monitoring.models import (
    BenchmarkSummaryResponse,
    BufferPoolStatisticsResponse,
    MonitoringStatusResponse,
    RecoveryStatisticsResponse,
    TransactionStatisticsResponse,
    WALStatisticsResponse,
    WorkloadSummaryResponse,
)
from app.recovery.recovery_manager import RecoveryManager
from app.recovery.recovery_state import RecoveryResult
from app.transaction.exceptions import TransactionManagerClosedError
from app.transaction.transaction_manager import TransactionManager
from app.wal.log_manager import LogManager
from app.workload.models import WorkloadResult


class MonitoringState:
    """Thread-safe registry for collecting database metrics without state mutation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._transaction_manager: Optional[TransactionManager] = None
        self._log_manager: Optional[LogManager] = None
        self._buffer_manager: Optional[BufferManager] = None
        self._checkpoint_manager: Optional[CheckpointManager] = None
        self._recovery_manager: Optional[RecoveryManager] = None
        self._latest_recovery_result: Optional[RecoveryResult] = None
        self._latest_recovery_stages: Optional[Dict[str, float]] = None
        self._latest_workload_result: Optional[WorkloadResult] = None
        self._latest_benchmark_result: Optional[BenchmarkScenarioResult] = None

    def register_database(
        self,
        transaction_manager: Optional[TransactionManager] = None,
        log_manager: Optional[LogManager] = None,
        buffer_manager: Optional[BufferManager] = None,
        checkpoint_manager: Optional[CheckpointManager] = None,
        recovery_manager: Optional[RecoveryManager] = None,
    ) -> None:
        """Register active database subsystem managers for monitoring."""
        with self._lock:
            if transaction_manager is not None:
                self._transaction_manager = transaction_manager
            if log_manager is not None:
                self._log_manager = log_manager
            if buffer_manager is not None:
                self._buffer_manager = buffer_manager
            if checkpoint_manager is not None:
                self._checkpoint_manager = checkpoint_manager
            if recovery_manager is not None:
                self._recovery_manager = recovery_manager

    def register_recovery_result(
        self,
        result: RecoveryResult,
        stage_durations: Optional[Dict[str, float]] = None,
    ) -> None:
        """Record the latest recovery outcome and optional measured stage durations."""
        with self._lock:
            self._latest_recovery_result = result
            self._latest_recovery_stages = dict(stage_durations) if stage_durations else None

    def register_workload_result(self, result: WorkloadResult) -> None:
        """Record the latest executed synthetic transaction workload result."""
        with self._lock:
            self._latest_workload_result = result

    def register_benchmark_result(self, result: BenchmarkScenarioResult) -> None:
        """Record the latest executed performance benchmark scenario result."""
        with self._lock:
            self._latest_benchmark_result = result

    def reset(self) -> None:
        """Reset all registered managers and results to uninitialized state."""
        with self._lock:
            self._transaction_manager = None
            self._log_manager = None
            self._buffer_manager = None
            self._checkpoint_manager = None
            self._recovery_manager = None
            self._latest_recovery_result = None
            self._latest_recovery_stages = None
            self._latest_workload_result = None
            self._latest_benchmark_result = None

    # -------------------------------------------------------------------------
    # Status and Component Extractors
    # -------------------------------------------------------------------------
    def get_status(self) -> MonitoringStatusResponse:
        """Generate overall service and database status."""
        with self._lock:
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            db_initialized = (
                self._transaction_manager is not None
                or self._buffer_manager is not None
                or self._log_manager is not None
            )

            # Determine recovery status first to inform overall service degradation
            rec_res = self._latest_recovery_result
            if rec_res is None and self._recovery_manager is not None:
                rec_res = self._recovery_manager.last_result

            if rec_res is None:
                if self._recovery_manager is not None and self._recovery_manager.is_recovered:
                    rec_status = "recovered"
                    latest_rec_success = True
                else:
                    rec_status = "not_run"
                    latest_rec_success = None
            elif rec_res.is_success:
                rec_status = "recovered"
                latest_rec_success = True
            else:
                rec_status = "failed"
                latest_rec_success = False

            if not db_initialized:
                status_str = "uninitialized"
            elif (
                (self._transaction_manager and self._transaction_manager.is_closed)
                or (self._buffer_manager and self._buffer_manager.is_closed)
                or (self._log_manager and self._log_manager.is_closed)
                or (rec_res is not None and not rec_res.is_success)
            ):
                status_str = "degraded"
            else:
                status_str = "operational"

            return MonitoringStatusResponse(
                service="ARIES Crash Recovery System",
                status=status_str,
                database_initialized=db_initialized,
                recovery_status=rec_status,
                latest_recovery_success=latest_rec_success,
                timestamp=now_iso,
            )

    def get_transaction_statistics(self) -> TransactionStatisticsResponse:
        """Extract read-only transaction manager statistics."""
        with self._lock:
            if self._transaction_manager is None:
                return TransactionStatisticsResponse(status="uninitialized")

            tm = self._transaction_manager
            try:
                if tm.is_closed:
                    # Access in-memory table directly if closed to avoid closed error
                    all_txs = list(tm._transactions.values())
                    started = len(tm._transactions)
                    committed = sum(1 for tx in all_txs if tx.is_committed)
                    aborted = sum(1 for tx in all_txs if tx.is_aborted)
                    active = 0
                    active_ids: List[int] = []
                else:
                    try:
                        tx_table = tm.get_transaction_table()
                        started = len(tx_table)
                        committed = sum(1 for tx in tx_table.values() if tx.is_committed)
                        aborted = sum(1 for tx in tx_table.values() if tx.is_aborted)
                        active_txs = tm.get_active_transactions()
                        active = len(active_txs)
                        active_ids = sorted([tx.transaction_id for tx in active_txs])
                    except TransactionManagerClosedError:
                        all_txs = list(tm._transactions.values())
                        started = len(tm._transactions)
                        committed = sum(1 for tx in all_txs if tx.is_committed)
                        aborted = sum(1 for tx in all_txs if tx.is_aborted)
                        active = 0
                        active_ids = []

                return TransactionStatisticsResponse(
                    status="available",
                    total_started=started,
                    total_committed=committed,
                    total_aborted=aborted,
                    active_count=active,
                    active_transaction_ids=active_ids,
                )
            except Exception:
                return TransactionStatisticsResponse(status="uninitialized")

    def get_wal_statistics(self) -> WALStatisticsResponse:
        """Extract read-only Write-Ahead Logging statistics."""
        with self._lock:
            if self._log_manager is None:
                return WALStatisticsResponse(status="uninitialized")

            lm = self._log_manager
            try:
                current_lsn = (lm.next_lsn - 1) if lm.next_lsn > 1 else 0
                flushed_lsn = lm.flushed_lsn
                total_records = len(lm._records)
                try:
                    file_size = (
                        os.path.getsize(lm.wal_path)
                        if (lm.wal_path and lm.wal_path.exists())
                        else 0
                    )
                except OSError:
                    file_size = 0
                is_closed = lm.is_closed

                return WALStatisticsResponse(
                    status="available",
                    current_lsn=current_lsn,
                    flushed_lsn=flushed_lsn,
                    total_records=total_records,
                    wal_file_size_bytes=file_size,
                    is_closed=is_closed,
                )
            except Exception:
                return WALStatisticsResponse(status="uninitialized")

    def get_buffer_pool_statistics(self) -> BufferPoolStatisticsResponse:
        """Extract read-only buffer pool statistics."""
        with self._lock:
            if self._buffer_manager is None:
                return BufferPoolStatisticsResponse(status="uninitialized")

            bm = self._buffer_manager
            try:
                pool_size = bm.pool_size
                is_closed = bm.is_closed

                if is_closed:
                    return BufferPoolStatisticsResponse(
                        status="available",
                        pool_size_frames=pool_size,
                        resident_pages_count=0,
                        pinned_frames_count=0,
                        dirty_pages_count=0,
                        available_frames_count=0,
                        is_closed=True,
                    )

                try:
                    resident_pages = list(bm.get_resident_pages())
                    resident_count = len(resident_pages)
                    pinned_count = 0
                    dirty_count = 0
                    for pid in resident_pages:
                        try:
                            if bm.get_pin_count(pid) > 0:
                                pinned_count += 1
                            if bm.is_dirty(pid):
                                dirty_count += 1
                        except PageNotResidentError:
                            pass
                    free_count = len(getattr(bm, "_free_frames", []))
                    evictable_count = bm._replacer.size() if hasattr(bm, "_replacer") else 0
                    available_frames = free_count + evictable_count
                except BufferManagerClosedError:
                    return BufferPoolStatisticsResponse(
                        status="available",
                        pool_size_frames=pool_size,
                        resident_pages_count=0,
                        pinned_frames_count=0,
                        dirty_pages_count=0,
                        available_frames_count=0,
                        is_closed=True,
                    )

                return BufferPoolStatisticsResponse(
                    status="available",
                    pool_size_frames=pool_size,
                    resident_pages_count=resident_count,
                    pinned_frames_count=pinned_count,
                    dirty_pages_count=dirty_count,
                    available_frames_count=available_frames,
                    is_closed=is_closed,
                )
            except Exception:
                return BufferPoolStatisticsResponse(status="uninitialized")

    def get_recovery_statistics(self) -> RecoveryStatisticsResponse:
        """Extract outcome of the latest crash recovery execution."""
        with self._lock:
            rec_res = self._latest_recovery_result
            if rec_res is None and self._recovery_manager is not None:
                rec_res = self._recovery_manager.last_result

            if rec_res is None:
                return RecoveryStatisticsResponse(status="not_run")

            stage_durs = self._latest_recovery_stages or {}

            status_str = "available" if rec_res.is_success else "failed"

            return RecoveryStatisticsResponse(
                status=status_str,
                is_success=rec_res.is_success,
                stage=rec_res.stage,
                error_message=rec_res.error_message,
                winners_count=len(rec_res.winners),
                losers_count=len(rec_res.losers),
                records_redone=rec_res.records_redone,
                clrs_generated=rec_res.clrs_generated,
                updates_undone=rec_res.updates_undone,
                pages_modified_count=len(rec_res.pages_modified),
                analysis_duration_s=stage_durs.get("analysis_duration_s"),
                redo_duration_s=stage_durs.get("redo_duration_s"),
                undo_duration_s=stage_durs.get("undo_duration_s"),
                total_duration_s=stage_durs.get("total_duration_s"),
            )

    def get_workload_summary(self) -> WorkloadSummaryResponse:
        """Extract summary of the latest executed transaction workload."""
        with self._lock:
            if self._latest_workload_result is None:
                return WorkloadSummaryResponse(status="not_available")

            wr = self._latest_workload_result
            return WorkloadSummaryResponse(
                status="available",
                planned_operations=wr.planned_operations,
                executed_operations=wr.executed_operations,
                transactions_started=wr.transactions_started,
                transactions_committed=wr.transactions_committed,
                transactions_aborted=wr.transactions_aborted,
                transactions_incomplete=wr.transactions_incomplete,
                reads_executed=wr.reads_executed,
                updates_executed=wr.updates_executed,
                is_valid=wr.is_valid,
                crash_configured=wr.crash_configured,
                crash_observed=wr.crash_observed,
                fault_point=wr.fault_point,
            )

    def get_benchmark_summary(self) -> BenchmarkSummaryResponse:
        """Extract summary of the latest executed benchmark scenario."""
        with self._lock:
            if self._latest_benchmark_result is None:
                return BenchmarkSummaryResponse(status="not_available")

            br = self._latest_benchmark_result
            aggs = br.aggregates

            return BenchmarkSummaryResponse(
                status="available",
                scenario_name=br.config.scenario_name,
                is_success=br.is_success,
                total_runs=len(br.all_runs),
                measured_runs_count=len(br.measured_runs),
                warmup_runs_count=len(br.warmup_runs),
                valid_runs_count=len(br.valid_runs),
                failed_runs_count=len(br.failed_runs),
                mean_workload_duration_s=(
                    aggs["workload_duration_s"].mean
                    if "workload_duration_s" in aggs
                    else None
                ),
                mean_tx_throughput_tps=(
                    aggs["tx_throughput_tps"].mean
                    if "tx_throughput_tps" in aggs
                    else None
                ),
                mean_op_throughput_ops=(
                    aggs["op_throughput_ops"].mean
                    if "op_throughput_ops" in aggs
                    else None
                ),
                mean_recovery_duration_s=(
                    aggs["recovery_duration_s"].mean
                    if "recovery_duration_s" in aggs
                    else None
                ),
            )


# Global singleton instance for application and test registration
monitoring_state = MonitoringState()
