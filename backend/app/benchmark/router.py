"""FastAPI router for bounded, safe performance benchmark execution."""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator

from app.benchmark.config import BenchmarkConfig
from app.benchmark.exceptions import (
    BenchmarkConfigError,
    BenchmarkError,
    BenchmarkExecutionError,
)
from app.benchmark.models import BenchmarkScenarioResult
from app.benchmark.runner import BenchmarkRunner
from app.benchmark.scenarios import get_preset_scenario, list_preset_scenarios
from app.monitoring.state import monitoring_state
from app.workload.router import workload_lock

logger = logging.getLogger("aries.benchmark.router")

router = APIRouter(prefix="/api/benchmark", tags=["benchmark"])


class BenchmarkRunRequest(BaseModel):
    """Request payload specifying parameters for benchmark execution."""

    scenario: str = Field(
        default="small_workload",
        description="Preset scenario name to run (e.g. 'small_workload', 'medium_workload', 'read_heavy').",
    )
    repetitions: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of measured benchmark repetitions (1 to 10).",
    )
    warmup_runs: int = Field(
        default=1,
        ge=0,
        le=5,
        description="Number of unmeasured warmup runs (0 to 5).",
    )
    seed: Optional[int] = Field(
        default=42,
        description="Deterministic random seed for plan reproducibility.",
    )
    timeout_per_run: Optional[float] = Field(
        default=30.0,
        ge=1.0,
        le=60.0,
        description="Execution timeout in seconds per individual run (1 to 60).",
    )

    @model_validator(mode="after")
    def validate_scenario_and_bounds(self) -> "BenchmarkRunRequest":
        """Validate that the scenario is known and total repetitions are within safe bounds."""
        valid_presets = list_preset_scenarios()
        if self.scenario not in valid_presets:
            raise ValueError(
                f"Unknown scenario '{self.scenario}'. Valid preset scenarios: {', '.join(valid_presets)}"
            )

        total_runs = self.repetitions + self.warmup_runs
        if total_runs > 12:
            raise ValueError(
                f"Total runs (repetitions={self.repetitions} + warmup_runs={self.warmup_runs} = {total_runs}) "
                f"exceeds maximum allowed limit of 12 runs."
            )
        return self


class BenchmarkRunResponse(BaseModel):
    """Response payload returned upon completing benchmark execution."""

    success: bool = Field(description="Whether the benchmark executed and all measured runs passed validation.")
    message: str = Field(description="Descriptive status message.")
    scenario_name: str = Field(description="Name of the executed benchmark scenario.")
    execution_mode: str = Field(
        default="isolated_temporary_storage",
        description="Identifies that benchmark runs were executed using isolated temporary storage per run.",
    )
    isolation_note: str = Field(
        default="Benchmark repetitions were executed using isolated per-run database instances to ensure pristine baselines and preserve live database durability.",
        description="Explanation of storage isolation semantics.",
    )
    total_runs: int = Field(description="Total runs executed (warmup + measured).")
    measured_runs_count: int = Field(description="Number of measured benchmark repetitions.")
    warmup_runs_count: int = Field(description="Number of warmup runs executed.")
    valid_runs_count: int = Field(description="Number of runs that passed correctness validation.")
    failed_runs_count: int = Field(description="Number of runs that failed validation.")
    is_success: bool = Field(description="True if all measured benchmark repetitions passed validation.")
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
    duration_seconds: float = Field(description="Total elapsed wall-clock duration of the benchmark execution.")
    aggregates: Dict[str, Any] = Field(
        default_factory=dict,
        description="Detailed statistical distribution aggregates (mean, median, min, max, std_dev, p95).",
    )


@router.get("/scenarios")
def get_benchmark_scenarios() -> Dict[str, Any]:
    """List all supported preset benchmark scenarios and default settings."""
    return {
        "scenarios": list_preset_scenarios(),
        "default_scenario": "small_workload",
        "limits": {
            "max_repetitions": 10,
            "max_warmup_runs": 5,
            "max_total_runs": 12,
            "max_timeout_per_run_s": 60.0,
        },
    }


@router.post("/run", response_model=BenchmarkRunResponse)
@router.post("/execute", response_model=BenchmarkRunResponse)
async def run_benchmark(
    payload: BenchmarkRunRequest,
    request: Request,
) -> BenchmarkRunResponse:
    """Execute bounded benchmark scenario safely, isolating storage per run and updating monitoring state.

    Enforces mutual exclusion with live workloads and other benchmarks via atomic lock.
    Verifies live database operational status prior to execution.
    Runs each repetition in an isolated temporary environment to preserve live database state.
    Registers result with monitoring_state upon validation success.
    """
    if not workload_lock.acquire(blocking=False):
        logger.warning("Rejected concurrent benchmark execution request (409 Conflict).")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A database workload or benchmark execution is currently running. Concurrent executions are not permitted.",
        )

    try:
        app = request.app
        tm = getattr(app.state, "transaction_manager", None)
        lm = getattr(app.state, "log_manager", None)
        bm = getattr(app.state, "buffer_manager", None)

        if tm is None or lm is None or bm is None or tm.is_closed or lm.is_closed or bm.is_closed:
            logger.error("Attempted benchmark run on uninitialized or closed database.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database subsystems are uninitialized or closed. Benchmark execution requires an operational live database server.",
            )

        try:
            config = get_preset_scenario(
                payload.scenario,
                repetitions=payload.repetitions,
                warmup_runs=payload.warmup_runs,
                seed=payload.seed,
            )
            if payload.timeout_per_run is not None:
                config._timeout_per_run = float(payload.timeout_per_run)
        except BenchmarkConfigError as exc:
            logger.warning(f"Invalid benchmark configuration: {exc}")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid benchmark configuration: {str(exc)}",
            )

        start_time = time.perf_counter()
        try:
            result: BenchmarkScenarioResult = await asyncio.to_thread(
                BenchmarkRunner.run,
                config=config,
            )
        except BenchmarkExecutionError as exc:
            logger.error(f"Benchmark execution failed: {exc}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Benchmark execution failed: {str(exc)}",
            )
        except Exception as exc:
            logger.error(f"Unexpected error during benchmark execution: {exc}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Unexpected error during benchmark execution: {type(exc).__name__}",
            )
        duration = time.perf_counter() - start_time

        # Validate that all measured repetitions passed correctness checks
        if not result.is_success or len(result.failed_runs) > 0:
            logger.warning(
                f"Benchmark validation failed for scenario '{config.scenario_name}': "
                f"{len(result.failed_runs)}/{len(result.measured_runs)} runs failed. "
                "Result will NOT be registered in monitoring state."
            )
            failed_errors = [
                f"Run {r.run_index}: {r.error_message or '; '.join(r.validation_errors)}"
                for r in result.failed_runs
            ]
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "Benchmark validation failed",
                    "scenario_name": config.scenario_name,
                    "failed_runs_count": len(result.failed_runs),
                    "valid_runs_count": len(result.valid_runs),
                    "validation_errors": failed_errors,
                },
            )

        # Register successful benchmark result with monitoring state
        monitoring_state.register_benchmark_result(result)
        logger.info(
            f"Benchmark scenario '{config.scenario_name}' completed and registered successfully: "
            f"{len(result.valid_runs)}/{len(result.measured_runs)} valid runs in {duration:.3f}s."
        )

        aggs = result.aggregates
        return BenchmarkRunResponse(
            success=True,
            message=f"Benchmark scenario '{config.scenario_name}' executed and validated successfully.",
            scenario_name=config.scenario_name,
            execution_mode="isolated_temporary_storage",
            isolation_note=(
                "Benchmark repetitions were executed using isolated per-run database instances "
                "to ensure pristine baselines and preserve live database durability."
            ),
            total_runs=len(result.all_runs),
            measured_runs_count=len(result.measured_runs),
            warmup_runs_count=len(result.warmup_runs),
            valid_runs_count=len(result.valid_runs),
            failed_runs_count=len(result.failed_runs),
            is_success=result.is_success,
            mean_workload_duration_s=(
                aggs["workload_duration_s"].mean if "workload_duration_s" in aggs else None
            ),
            mean_tx_throughput_tps=(
                aggs["tx_throughput_tps"].mean if "tx_throughput_tps" in aggs else None
            ),
            mean_op_throughput_ops=(
                aggs["op_throughput_ops"].mean if "op_throughput_ops" in aggs else None
            ),
            mean_recovery_duration_s=(
                aggs["recovery_duration_s"].mean if "recovery_duration_s" in aggs else None
            ),
            duration_seconds=duration,
            aggregates={k: v.to_dict() for k, v in aggs.items()},
        )
    finally:
        workload_lock.release()
