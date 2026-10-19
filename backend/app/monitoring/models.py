"""Pydantic response models for read-only database monitoring and statistics APIs."""

from typing import List, Optional
from pydantic import BaseModel, ConfigDict, Field


class MonitoringStatusResponse(BaseModel):
    """Overall status and health summary of the database engine and recovery subsystem."""

    model_config = ConfigDict(extra="forbid")

    service: str = Field(
        default="ARIES Crash Recovery System",
        description="Name of the database service.",
    )
    status: str = Field(
        description="Current operational status ('operational', 'degraded', or 'uninitialized').",
    )
    database_initialized: bool = Field(
        description="True if database managers (transaction, WAL, buffer) are active and registered.",
    )
    recovery_status: str = Field(
        description="Status of crash recovery ('not_run', 'recovered', 'unrecovered', or 'failed').",
    )
    latest_recovery_success: Optional[bool] = Field(
        default=None,
        description="True if the most recent recovery succeeded, False if it failed, or None if not run.",
    )
    timestamp: str = Field(
        description="ISO-8601 UTC timestamp of the monitoring check.",
    )


class TransactionStatisticsResponse(BaseModel):
    """Statistics covering active, committed, aborted, and started transactions."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(
        description="Data availability status ('available' or 'uninitialized').",
    )
    total_started: Optional[int] = Field(
        default=None,
        description="Total number of transactions started in current lifecycle.",
    )
    total_committed: Optional[int] = Field(
        default=None,
        description="Total number of successfully committed transactions.",
    )
    total_aborted: Optional[int] = Field(
        default=None,
        description="Total number of aborted transactions.",
    )
    active_count: Optional[int] = Field(
        default=None,
        description="Count of transactions currently in RUNNING state.",
    )
    active_transaction_ids: Optional[List[int]] = Field(
        default=None,
        description="Sorted list of transaction IDs currently in RUNNING state.",
    )


class WALStatisticsResponse(BaseModel):
    """Statistics covering Write-Ahead Logging state, LSN progress, and log volume."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(
        description="Data availability status ('available' or 'uninitialized').",
    )
    current_lsn: Optional[int] = Field(
        default=None,
        description="Most recently generated LSN (monotonic sequence number).",
    )
    flushed_lsn: Optional[int] = Field(
        default=None,
        description="Highest LSN flushed and fsynced to persistent storage.",
    )
    total_records: Optional[int] = Field(
        default=None,
        description="Total count of log records in the active WAL.",
    )
    wal_file_size_bytes: Optional[int] = Field(
        default=None,
        description="Size of the persistent WAL file in bytes on disk.",
    )
    is_closed: Optional[bool] = Field(
        default=None,
        description="True if LogManager is closed, False if open.",
    )


class BufferPoolStatisticsResponse(BaseModel):
    """Statistics covering buffer pool capacity, residence, pins, and dirty frames."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(
        description="Data availability status ('available' or 'uninitialized').",
    )
    pool_size_frames: Optional[int] = Field(
        default=None,
        description="Total buffer pool frame capacity (unit: frames).",
    )
    resident_pages_count: Optional[int] = Field(
        default=None,
        description="Number of database pages currently cached in memory (unit: pages).",
    )
    pinned_frames_count: Optional[int] = Field(
        default=None,
        description="Number of buffer pool frames with active pin count > 0 (unit: frames).",
    )
    dirty_pages_count: Optional[int] = Field(
        default=None,
        description="Number of resident pages with unflushed modifications (unit: pages).",
    )
    available_frames_count: Optional[int] = Field(
        default=None,
        description="Number of unallocated or unpinned frames eligible for replacement (unit: frames).",
    )
    is_closed: Optional[bool] = Field(
        default=None,
        description="True if BufferManager is closed, False if open.",
    )


class RecoveryStatisticsResponse(BaseModel):
    """Statistics and outcome of the latest ARIES crash recovery execution."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(
        description="Recovery execution state ('not_run', 'available', or 'failed').",
    )
    is_success: Optional[bool] = Field(
        default=None,
        description="True if recovery completed successfully, False if failed.",
    )
    stage: Optional[str] = Field(
        default=None,
        description="Final completed recovery stage or stage of failure.",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Failure reason if recovery encountered an error, or None.",
    )
    winners_count: Optional[int] = Field(
        default=None,
        description="Number of winner transactions identified during Analysis.",
    )
    losers_count: Optional[int] = Field(
        default=None,
        description="Number of loser transactions identified during Analysis.",
    )
    records_redone: Optional[int] = Field(
        default=None,
        description="Number of log records replayed during Redo.",
    )
    clrs_generated: Optional[int] = Field(
        default=None,
        description="Number of Compensation Log Records (CLRs) written during Undo.",
    )
    updates_undone: Optional[int] = Field(
        default=None,
        description="Number of UPDATE records rolled back during Undo.",
    )
    pages_modified_count: Optional[int] = Field(
        default=None,
        description="Number of distinct database pages modified during Redo and Undo.",
    )
    analysis_duration_s: Optional[float] = Field(
        default=None,
        description="Measured duration of the Analysis phase in seconds, or None if unmeasured.",
    )
    redo_duration_s: Optional[float] = Field(
        default=None,
        description="Measured duration of the Redo phase in seconds, or None if unmeasured.",
    )
    undo_duration_s: Optional[float] = Field(
        default=None,
        description="Measured duration of the Undo phase in seconds, or None if unmeasured.",
    )
    total_duration_s: Optional[float] = Field(
        default=None,
        description="Measured total recovery duration in seconds, or None if unmeasured.",
    )


class WorkloadSummaryResponse(BaseModel):
    """Read-only summary of the most recently executed synthetic transaction workload."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(
        description="Data availability status ('available' or 'not_available').",
    )
    planned_operations: Optional[int] = Field(
        default=None,
        description="Total planned operations in the workload plan.",
    )
    executed_operations: Optional[int] = Field(
        default=None,
        description="Total operations dispatched and executed.",
    )
    transactions_started: Optional[int] = Field(
        default=None,
        description="Number of transactions initiated in the workload.",
    )
    transactions_committed: Optional[int] = Field(
        default=None,
        description="Number of transactions committed in the workload.",
    )
    transactions_aborted: Optional[int] = Field(
        default=None,
        description="Number of transactions aborted in the workload.",
    )
    transactions_incomplete: Optional[int] = Field(
        default=None,
        description="Number of transactions left incomplete/uncommitted in the workload.",
    )
    reads_executed: Optional[int] = Field(
        default=None,
        description="Number of READ operations executed.",
    )
    updates_executed: Optional[int] = Field(
        default=None,
        description="Number of UPDATE operations executed.",
    )
    is_valid: Optional[bool] = Field(
        default=None,
        description="True if post-workload database correctness validation passed.",
    )
    crash_configured: Optional[bool] = Field(
        default=None,
        description="True if a fault injection crash point was configured.",
    )
    crash_observed: Optional[bool] = Field(
        default=None,
        description="True if process crash termination occurred as expected.",
    )
    fault_point: Optional[str] = Field(
        default=None,
        description="Name of configured fault point, if any.",
    )


class BenchmarkSummaryResponse(BaseModel):
    """Read-only summary of the most recently executed performance benchmark scenario."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(
        description="Data availability status ('available' or 'not_available').",
    )
    scenario_name: Optional[str] = Field(
        default=None,
        description="Identifier of the benchmark scenario.",
    )
    is_success: Optional[bool] = Field(
        default=None,
        description="True if all measured benchmark repetitions passed validation.",
    )
    total_runs: Optional[int] = Field(
        default=None,
        description="Total runs executed (warmup + measured).",
    )
    measured_runs_count: Optional[int] = Field(
        default=None,
        description="Number of measured benchmark repetitions.",
    )
    warmup_runs_count: Optional[int] = Field(
        default=None,
        description="Number of warmup runs executed.",
    )
    valid_runs_count: Optional[int] = Field(
        default=None,
        description="Number of runs that passed correctness validation.",
    )
    failed_runs_count: Optional[int] = Field(
        default=None,
        description="Number of runs that failed validation.",
    )
    mean_workload_duration_s: Optional[float] = Field(
        default=None,
        description="Mean workload execution duration in seconds across measured runs.",
    )
    mean_tx_throughput_tps: Optional[float] = Field(
        default=None,
        description="Mean transaction throughput in completed transactions per second.",
    )
    mean_op_throughput_ops: Optional[float] = Field(
        default=None,
        description="Mean operation throughput in operations per second.",
    )
    mean_recovery_duration_s: Optional[float] = Field(
        default=None,
        description="Mean recovery duration in seconds across measured runs, if applicable.",
    )
