"""Configuration and validation for benchmark scenarios and execution."""

from typing import Any, Dict, Optional
from app.benchmark.exceptions import BenchmarkConfigError
from app.workload.config import WorkloadConfig


class BenchmarkConfig:
    """Configuration governing benchmark execution, repetitions, and metrics collection.

    Attributes:
        scenario_name: Unique human-readable name identifying this scenario.
        repetitions: Number of measured benchmark runs (>= 1).
        warmup_runs: Number of unmeasured warmup runs executed prior to measurement (>= 0).
        seed: Deterministic random seed for reproducibility.
        workload_config: Underlying WorkloadConfig for generating transaction operations.
        is_recovery_only: If True, measures recovery from pre-generated crash files.
        measure_recovery_stages: If True, measures Analysis, Redo, and Undo separately.
        collect_environment_metadata: If True, records platform and runtime environment details.
        timeout_per_run: Timeout in seconds for any individual run (including crash experiments).
    """

    def __init__(
        self,
        scenario_name: str = "default_benchmark",
        repetitions: int = 3,
        warmup_runs: int = 1,
        seed: Optional[int] = 42,
        workload_config: Optional[WorkloadConfig] = None,
        is_recovery_only: bool = False,
        measure_recovery_stages: bool = True,
        collect_environment_metadata: bool = True,
        timeout_per_run: float = 30.0,
    ) -> None:
        """Initialize and validate BenchmarkConfig."""
        self._validate_and_set(
            scenario_name=scenario_name,
            repetitions=repetitions,
            warmup_runs=warmup_runs,
            seed=seed,
            workload_config=workload_config,
            is_recovery_only=is_recovery_only,
            measure_recovery_stages=measure_recovery_stages,
            collect_environment_metadata=collect_environment_metadata,
            timeout_per_run=timeout_per_run,
        )

    def _validate_and_set(
        self,
        scenario_name: str,
        repetitions: int,
        warmup_runs: int,
        seed: Optional[int],
        workload_config: Optional[WorkloadConfig],
        is_recovery_only: bool,
        measure_recovery_stages: bool,
        collect_environment_metadata: bool,
        timeout_per_run: float,
    ) -> None:
        """Validate input values and assign fields."""
        if not isinstance(scenario_name, str) or not scenario_name.strip():
            raise BenchmarkConfigError(
                f"scenario_name must be a non-empty string, got {scenario_name!r}"
            )

        if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions < 1:
            raise BenchmarkConfigError(
                f"repetitions must be an integer >= 1, got {repetitions!r}"
            )

        if not isinstance(warmup_runs, int) or isinstance(warmup_runs, bool) or warmup_runs < 0:
            raise BenchmarkConfigError(
                f"warmup_runs must be a non-negative integer, got {warmup_runs!r}"
            )

        if seed is not None:
            if not isinstance(seed, int) or isinstance(seed, bool):
                raise BenchmarkConfigError(
                    f"seed must be an integer or None, got {seed!r}"
                )

        if not isinstance(is_recovery_only, bool):
            raise BenchmarkConfigError(
                f"is_recovery_only must be a boolean, got {is_recovery_only!r}"
            )

        if not isinstance(measure_recovery_stages, bool):
            raise BenchmarkConfigError(
                f"measure_recovery_stages must be a boolean, got {measure_recovery_stages!r}"
            )

        if not isinstance(collect_environment_metadata, bool):
            raise BenchmarkConfigError(
                f"collect_environment_metadata must be a boolean, got {collect_environment_metadata!r}"
            )

        if not isinstance(timeout_per_run, (int, float)) or isinstance(timeout_per_run, bool) or timeout_per_run <= 0.0:
            raise BenchmarkConfigError(
                f"timeout_per_run must be a positive float, got {timeout_per_run!r}"
            )

        if workload_config is not None and not isinstance(workload_config, WorkloadConfig):
            raise BenchmarkConfigError(
                f"workload_config must be an instance of WorkloadConfig, got {type(workload_config).__name__}"
            )

        # Default workload config if none provided
        effective_workload_config = (
            workload_config
            if workload_config is not None
            else WorkloadConfig(seed=seed)
        )

        self._scenario_name: str = scenario_name.strip()
        self._repetitions: int = repetitions
        self._warmup_runs: int = warmup_runs
        self._seed: Optional[int] = seed
        self._workload_config: WorkloadConfig = effective_workload_config
        self._is_recovery_only: bool = is_recovery_only
        self._measure_recovery_stages: bool = measure_recovery_stages
        self._collect_environment_metadata: bool = collect_environment_metadata
        self._timeout_per_run: float = float(timeout_per_run)

    @property
    def scenario_name(self) -> str:
        """Return the scenario name."""
        return self._scenario_name

    @property
    def repetitions(self) -> int:
        """Return the number of measured repetitions."""
        return self._repetitions

    @property
    def warmup_runs(self) -> int:
        """Return the number of warmup runs."""
        return self._warmup_runs

    @property
    def seed(self) -> Optional[int]:
        """Return the base random seed."""
        return self._seed

    @property
    def workload_config(self) -> WorkloadConfig:
        """Return the underlying WorkloadConfig."""
        return self._workload_config

    @property
    def is_recovery_only(self) -> bool:
        """Return whether this scenario tests recovery alone."""
        return self._is_recovery_only

    @property
    def measure_recovery_stages(self) -> bool:
        """Return whether recovery stages (Analysis, Redo, Undo) are timed individually."""
        return self._measure_recovery_stages

    @property
    def collect_environment_metadata(self) -> bool:
        """Return whether environment metadata is collected."""
        return self._collect_environment_metadata

    @property
    def timeout_per_run(self) -> float:
        """Return the timeout per run in seconds."""
        return self._timeout_per_run

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary representation."""
        return {
            "scenario_name": self._scenario_name,
            "repetitions": self._repetitions,
            "warmup_runs": self._warmup_runs,
            "seed": self._seed,
            "is_recovery_only": self._is_recovery_only,
            "measure_recovery_stages": self._measure_recovery_stages,
            "collect_environment_metadata": self._collect_environment_metadata,
            "timeout_per_run": self._timeout_per_run,
            "workload_config": self._workload_config.to_dict(),
        }

    def __repr__(self) -> str:
        return (
            f"BenchmarkConfig(scenario='{self._scenario_name}', runs={self._repetitions}, "
            f"warmup={self._warmup_runs}, recovery_only={self._is_recovery_only})"
        )
