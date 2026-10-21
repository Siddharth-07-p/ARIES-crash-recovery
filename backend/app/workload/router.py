import asyncio
import logging
import threading
import time
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from app.monitoring.state import monitoring_state
from app.workload.config import WorkloadConfig
from app.workload.exceptions import (
    WorkloadConfigError,
    WorkloadError,
    WorkloadExecutionError,
    WorkloadValidationError,
)
from app.workload.executor import WorkloadExecutor
from app.workload.models import WorkloadResult

logger = logging.getLogger("aries.workload.router")

router = APIRouter(prefix="/api/workload", tags=["workload"])

# Concurrency guard ensuring single active workload execution across requests
workload_lock = threading.Lock()


class WorkloadRunRequest(BaseModel):
    """Request payload specifying parameters for live synthetic workload execution."""

    num_transactions: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Total number of transactions to generate (1 to 100).",
    )
    num_pages: int = Field(
        default=4,
        ge=1,
        le=32,
        description="Number of accessible database pages (1 to 32).",
    )
    operations_per_transaction: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of read/update operations per transaction (1 to 20).",
    )
    read_ratio: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="Probability of an operation being a READ (0.0 to 1.0).",
    )
    commit_ratio: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="Probability of a transaction committing (0.0 to 1.0).",
    )
    abort_ratio: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="Probability of a transaction aborting (0.0 to 1.0).",
    )
    incomplete_ratio: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="Probability of a transaction left incomplete (0.0 to 1.0).",
    )
    same_page_probability: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="Probability of targeting page 0 (0.0 to 1.0).",
    )
    interleave_probability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Probability of interleaving operations across transactions (0.0 to 1.0).",
    )
    seed: Optional[int] = Field(
        default=None,
        description="Deterministic integer seed for reproducible plan generation.",
    )
    checkpoint_interval: Optional[int] = Field(
        default=None,
        ge=1,
        le=500,
        description="Interval in operations at which to trigger checkpoints.",
    )

    @model_validator(mode="after")
    def validate_bounds_and_ratios(self) -> "WorkloadRunRequest":
        """Validate operational limits and probability distributions."""
        total_ops = self.num_transactions * self.operations_per_transaction
        if total_ops > 1000:
            raise ValueError(
                f"Total operations ({total_ops}) exceeds maximum allowed limit of 1000."
            )

        ratio_sum = self.commit_ratio + self.abort_ratio + self.incomplete_ratio
        if abs(ratio_sum - 1.0) > 0.001:
            raise ValueError(
                f"Outcome ratios (commit={self.commit_ratio}, abort={self.abort_ratio}, "
                f"incomplete={self.incomplete_ratio}) must sum to 1.0 (got {ratio_sum:.3f})."
            )
        return self


class WorkloadRunResponse(BaseModel):
    """Response payload returned upon completing synthetic workload execution."""

    success: bool = Field(description="Whether the workload executed and passed all validations.")
    message: str = Field(description="Descriptive status message.")
    planned_operations: int = Field(description="Total operations planned.")
    executed_operations: int = Field(description="Total operations executed.")
    transactions_started: int = Field(description="Transactions started.")
    transactions_committed: int = Field(description="Transactions committed.")
    transactions_aborted: int = Field(description="Transactions aborted.")
    transactions_incomplete: int = Field(description="Transactions left active/incomplete.")
    reads_executed: int = Field(description="Read operations executed.")
    updates_executed: int = Field(description="Update operations executed.")
    is_valid: bool = Field(description="Whether database state validation passed.")
    validation_errors: List[str] = Field(
        default_factory=list, description="Validation error details if any."
    )
    committed_transactions: List[int] = Field(
        default_factory=list, description="IDs of committed transactions."
    )
    aborted_transactions: List[int] = Field(
        default_factory=list, description="IDs of aborted transactions."
    )
    incomplete_transactions: List[int] = Field(
        default_factory=list, description="IDs of incomplete transactions."
    )
    duration_seconds: float = Field(description="Execution duration in seconds.")


@router.post("/run", response_model=WorkloadRunResponse)
@router.post("/execute", response_model=WorkloadRunResponse)
async def run_workload(
    payload: WorkloadRunRequest,
    request: Request,
) -> WorkloadRunResponse:
    """Execute synthetic transaction workload safely against the initialized live database instance.

    Rejects concurrent runs with 409 Conflict.
    Validates request bounds and outcome distributions.
    Executes operations directly on active TransactionManager, BufferManager, LogManager,
    and CheckpointManager instances.
    Registers the result with monitoring_state upon successful validation.
    """
    if not workload_lock.acquire(blocking=False):
        logger.warning("Rejected concurrent workload execution request (409 Conflict).")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A synthetic workload is currently running. Concurrent executions are not permitted.",
        )

    try:
        app = request.app
        tm = getattr(app.state, "transaction_manager", None)
        lm = getattr(app.state, "log_manager", None)
        bm = getattr(app.state, "buffer_manager", None)
        cm = getattr(app.state, "checkpoint_manager", None)

        if tm is None or lm is None or bm is None or tm.is_closed or lm.is_closed or bm.is_closed:
            logger.error("Attempted workload run on uninitialized or closed database.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database subsystems are uninitialized or closed. Workload execution requires an operational live database.",
            )

        try:
            config = WorkloadConfig(
                num_transactions=payload.num_transactions,
                num_pages=payload.num_pages,
                operations_per_transaction=payload.operations_per_transaction,
                read_ratio=payload.read_ratio,
                commit_ratio=payload.commit_ratio,
                abort_ratio=payload.abort_ratio,
                incomplete_ratio=payload.incomplete_ratio,
                same_page_probability=payload.same_page_probability,
                interleave_probability=payload.interleave_probability,
                seed=payload.seed,
                pool_size=bm.pool_size,
                crash_fault_point=None,
                auto_recover=False,
                checkpoint_interval=payload.checkpoint_interval,
            )
        except WorkloadConfigError as exc:
            logger.warning(f"Invalid workload configuration: {exc}")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid workload configuration: {str(exc)}",
            )

        start_time = time.perf_counter()
        try:
            result: WorkloadResult = await asyncio.to_thread(
                WorkloadExecutor.execute_live,
                config=config,
                transaction_manager=tm,
                log_manager=lm,
                buffer_manager=bm,
                checkpoint_manager=cm,
            )
        except WorkloadExecutionError as exc:
            logger.error(f"Workload execution failed: {exc}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Workload execution failed: {str(exc)}",
            )
        except Exception as exc:
            logger.error(f"Unexpected database error during workload execution: {exc}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unexpected error during workload execution: {type(exc).__name__}",
            )
        duration = time.perf_counter() - start_time

        # Validate that execution and validation both succeeded
        if not result.is_valid:
            logger.warning(
                f"Workload validation failed with errors: {result.validation_errors}. "
                "Result will NOT be registered as successful monitoring state."
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "Workload database state validation failed",
                    "validation_errors": result.validation_errors,
                    "executed_operations": result.executed_operations,
                },
            )

        # Register successful workload result for monitoring dashboard
        monitoring_state.register_workload_result(result)
        logger.info(
            f"Workload executed and validated successfully: {result.executed_operations} ops, "
            f"{result.transactions_committed} committed, {result.transactions_aborted} aborted, "
            f"{result.transactions_incomplete} incomplete in {duration:.3f}s."
        )

        return WorkloadRunResponse(
            success=True,
            message="Workload executed and validated successfully against live database.",
            planned_operations=result.planned_operations,
            executed_operations=result.executed_operations,
            transactions_started=result.transactions_started,
            transactions_committed=result.transactions_committed,
            transactions_aborted=result.transactions_aborted,
            transactions_incomplete=result.transactions_incomplete,
            reads_executed=result.reads_executed,
            updates_executed=result.updates_executed,
            is_valid=result.is_valid,
            validation_errors=result.validation_errors,
            committed_transactions=result.committed_transactions,
            aborted_transactions=result.aborted_transactions,
            incomplete_transactions=result.incomplete_transactions,
            duration_seconds=duration,
        )
    finally:
        workload_lock.release()
