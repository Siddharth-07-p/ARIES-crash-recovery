"""Standard preset benchmark scenarios covering workloads, recovery, and contention."""

from typing import Any, Dict, List, Optional
from app.benchmark.config import BenchmarkConfig
from app.benchmark.exceptions import BenchmarkConfigError
from app.fault.constants import FaultPoint
from app.workload.config import WorkloadConfig


def small_workload(
    repetitions: int = 3,
    warmup_runs: int = 1,
    seed: Optional[int] = 42,
) -> BenchmarkConfig:
    """Small transaction workload scenario for quick sanity checks and unit tests."""
    return BenchmarkConfig(
        scenario_name="small_workload",
        repetitions=repetitions,
        warmup_runs=warmup_runs,
        seed=seed,
        workload_config=WorkloadConfig(
            num_transactions=5,
            num_pages=2,
            operations_per_transaction=3,
            read_ratio=0.3,
            commit_ratio=0.6,
            abort_ratio=0.2,
            incomplete_ratio=0.2,
            seed=seed,
        ),
    )


def medium_workload(
    repetitions: int = 3,
    warmup_runs: int = 1,
    seed: Optional[int] = 42,
) -> BenchmarkConfig:
    """Medium-sized balanced workload."""
    return BenchmarkConfig(
        scenario_name="medium_workload",
        repetitions=repetitions,
        warmup_runs=warmup_runs,
        seed=seed,
        workload_config=WorkloadConfig(
            num_transactions=20,
            num_pages=8,
            operations_per_transaction=5,
            read_ratio=0.3,
            commit_ratio=0.7,
            abort_ratio=0.2,
            incomplete_ratio=0.1,
            seed=seed,
        ),
    )


def large_workload(
    repetitions: int = 3,
    warmup_runs: int = 1,
    seed: Optional[int] = 42,
) -> BenchmarkConfig:
    """Large transaction workload measuring throughput under heavier operation loads."""
    return BenchmarkConfig(
        scenario_name="large_workload",
        repetitions=repetitions,
        warmup_runs=warmup_runs,
        seed=seed,
        workload_config=WorkloadConfig(
            num_transactions=50,
            num_pages=16,
            operations_per_transaction=8,
            read_ratio=0.3,
            commit_ratio=0.8,
            abort_ratio=0.1,
            incomplete_ratio=0.1,
            seed=seed,
        ),
    )


def read_heavy(
    repetitions: int = 3,
    warmup_runs: int = 1,
    seed: Optional[int] = 42,
) -> BenchmarkConfig:
    """Read-heavy workload mix (85% reads, 15% updates)."""
    return BenchmarkConfig(
        scenario_name="read_heavy",
        repetitions=repetitions,
        warmup_runs=warmup_runs,
        seed=seed,
        workload_config=WorkloadConfig(
            num_transactions=20,
            num_pages=8,
            operations_per_transaction=6,
            read_ratio=0.85,
            commit_ratio=0.8,
            abort_ratio=0.1,
            incomplete_ratio=0.1,
            seed=seed,
        ),
    )


def update_heavy(
    repetitions: int = 3,
    warmup_runs: int = 1,
    seed: Optional[int] = 42,
) -> BenchmarkConfig:
    """Update-heavy workload mix (10% reads, 90% updates) generating high WAL traffic."""
    return BenchmarkConfig(
        scenario_name="update_heavy",
        repetitions=repetitions,
        warmup_runs=warmup_runs,
        seed=seed,
        workload_config=WorkloadConfig(
            num_transactions=20,
            num_pages=8,
            operations_per_transaction=6,
            read_ratio=0.10,
            commit_ratio=0.8,
            abort_ratio=0.1,
            incomplete_ratio=0.1,
            seed=seed,
        ),
    )


def hotspot_contention(
    repetitions: int = 3,
    warmup_runs: int = 1,
    seed: Optional[int] = 42,
) -> BenchmarkConfig:
    """High hotspot contention workload targeting Page 0 with 80% probability."""
    return BenchmarkConfig(
        scenario_name="hotspot_contention",
        repetitions=repetitions,
        warmup_runs=warmup_runs,
        seed=seed,
        workload_config=WorkloadConfig(
            num_transactions=20,
            num_pages=8,
            operations_per_transaction=5,
            same_page_probability=0.8,
            commit_ratio=0.7,
            abort_ratio=0.3,
            incomplete_ratio=0.0,
            seed=seed,
        ),
    )


def interleaved_concurrent(
    repetitions: int = 3,
    warmup_runs: int = 1,
    seed: Optional[int] = 42,
) -> BenchmarkConfig:
    """Workload with 100% interleaved operations across concurrent transactions."""
    return BenchmarkConfig(
        scenario_name="interleaved_concurrent",
        repetitions=repetitions,
        warmup_runs=warmup_runs,
        seed=seed,
        workload_config=WorkloadConfig(
            num_transactions=15,
            num_pages=4,
            operations_per_transaction=4,
            interleave_probability=1.0,
            commit_ratio=0.7,
            abort_ratio=0.2,
            incomplete_ratio=0.1,
            seed=seed,
        ),
    )


def high_abort(
    repetitions: int = 3,
    warmup_runs: int = 1,
    seed: Optional[int] = 42,
) -> BenchmarkConfig:
    """Workload with high abort frequency (70% aborts) measuring runtime rollback/CLR cost."""
    return BenchmarkConfig(
        scenario_name="high_abort",
        repetitions=repetitions,
        warmup_runs=warmup_runs,
        seed=seed,
        workload_config=WorkloadConfig(
            num_transactions=20,
            num_pages=4,
            operations_per_transaction=5,
            read_ratio=0.2,
            commit_ratio=0.2,
            abort_ratio=0.7,
            incomplete_ratio=0.1,
            seed=seed,
        ),
    )


def recovery_only(
    repetitions: int = 3,
    warmup_runs: int = 1,
    seed: Optional[int] = 42,
) -> BenchmarkConfig:
    """Recovery-only experiment: prepares crash state outside timed interval, then measures recovery."""
    return BenchmarkConfig(
        scenario_name="recovery_only",
        repetitions=repetitions,
        warmup_runs=warmup_runs,
        seed=seed,
        is_recovery_only=True,
        measure_recovery_stages=True,
        workload_config=WorkloadConfig(
            num_transactions=15,
            num_pages=4,
            operations_per_transaction=5,
            read_ratio=0.2,
            commit_ratio=0.5,
            abort_ratio=0.0,
            incomplete_ratio=0.5,
            auto_recover=False,
            seed=seed,
        ),
    )


def crash_recovery_commit_fault(
    repetitions: int = 3,
    warmup_runs: int = 0,
    seed: Optional[int] = 42,
) -> BenchmarkConfig:
    """Optional crash-recovery experiment using FaultPoint.DURING_TRANSACTION_COMMIT."""
    return BenchmarkConfig(
        scenario_name="crash_recovery_commit_fault",
        repetitions=repetitions,
        warmup_runs=warmup_runs,
        seed=seed,
        workload_config=WorkloadConfig(
            num_transactions=10,
            num_pages=4,
            operations_per_transaction=4,
            read_ratio=0.2,
            commit_ratio=1.0,
            abort_ratio=0.0,
            incomplete_ratio=0.0,
            crash_fault_point=FaultPoint.DURING_TRANSACTION_COMMIT,
            crash_exit_code=42,
            auto_recover=True,
            seed=seed,
        ),
    )


PRESET_SCENARIOS: Dict[str, Any] = {
    "small_workload": small_workload,
    "medium_workload": medium_workload,
    "large_workload": large_workload,
    "read_heavy": read_heavy,
    "update_heavy": update_heavy,
    "hotspot_contention": hotspot_contention,
    "interleaved_concurrent": interleaved_concurrent,
    "high_abort": high_abort,
    "recovery_only": recovery_only,
    "crash_recovery_commit_fault": crash_recovery_commit_fault,
}


def get_preset_scenario(name: str, **kwargs: Any) -> BenchmarkConfig:
    """Retrieve a BenchmarkConfig for the specified preset scenario name.

    Args:
        name: Name of the preset scenario.
        **kwargs: Optional configuration overrides (repetitions, warmup_runs, seed).

    Returns:
        Configured BenchmarkConfig.

    Raises:
        BenchmarkConfigError: If scenario name is unknown.
    """
    factory = PRESET_SCENARIOS.get(name)
    if factory is None:
        valid_names = ", ".join(PRESET_SCENARIOS.keys())
        raise BenchmarkConfigError(
            f"Unknown benchmark scenario: {name!r}. Valid options: {valid_names}"
        )
    return factory(**kwargs)


def list_preset_scenarios() -> List[str]:
    """Return list of all registered preset scenario names."""
    return list(PRESET_SCENARIOS.keys())
