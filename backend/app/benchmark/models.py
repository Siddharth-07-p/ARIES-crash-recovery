"""Data models representing benchmark metrics, run outcomes, and aggregate statistics."""

from dataclasses import dataclass
import os
import platform
import statistics
import sys
import time
from typing import Any, Dict, List, Optional
from app.benchmark.config import BenchmarkConfig


@dataclass(frozen=True)
class EnvironmentMetadata:
    """Hardware and execution environment metadata."""

    timestamp: str
    python_version: str
    python_implementation: str
    os_name: str
    os_release: str
    architecture: str
    cpu_count: int

    @classmethod
    def capture(cls) -> "EnvironmentMetadata":
        """Capture the current environment properties."""
        return cls(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            python_version=sys.version.split()[0],
            python_implementation=platform.python_implementation(),
            os_name=platform.system(),
            os_release=platform.release(),
            architecture=platform.machine(),
            cpu_count=os.cpu_count() or 1,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert metadata to dictionary representation."""
        return {
            "timestamp": self.timestamp,
            "python_version": self.python_version,
            "python_implementation": self.python_implementation,
            "os_name": self.os_name,
            "os_release": self.os_release,
            "architecture": self.architecture,
            "cpu_count": self.cpu_count,
        }


@dataclass(frozen=True)
class WorkloadRunMetrics:
    """Metrics captured during the timed workload execution interval."""

    duration_s: float
    transactions_started: int
    transactions_committed: int
    transactions_aborted: int
    transactions_incomplete: int
    completed_transactions: int
    executed_operations: int
    reads_executed: int
    updates_executed: int
    tx_throughput_tps: float
    committed_tps: float
    op_throughput_ops: float

    @classmethod
    def create(
        cls,
        duration_s: float,
        transactions_started: int,
        transactions_committed: int,
        transactions_aborted: int,
        transactions_incomplete: int,
        reads_executed: int,
        updates_executed: int,
        executed_operations: int,
    ) -> "WorkloadRunMetrics":
        """Compute throughput metrics and construct WorkloadRunMetrics."""
        safe_duration = max(0.0, float(duration_s))
        completed = transactions_committed + transactions_aborted

        if safe_duration > 0.0:
            tx_throughput = completed / safe_duration
            committed_throughput = transactions_committed / safe_duration
            op_throughput = executed_operations / safe_duration
        else:
            tx_throughput = 0.0
            committed_throughput = 0.0
            op_throughput = 0.0

        return cls(
            duration_s=safe_duration,
            transactions_started=transactions_started,
            transactions_committed=transactions_committed,
            transactions_aborted=transactions_aborted,
            transactions_incomplete=transactions_incomplete,
            completed_transactions=completed,
            executed_operations=executed_operations,
            reads_executed=reads_executed,
            updates_executed=updates_executed,
            tx_throughput_tps=round(tx_throughput, 2),
            committed_tps=round(committed_throughput, 2),
            op_throughput_ops=round(op_throughput, 2),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert workload metrics to dictionary representation."""
        return {
            "duration_s": self.duration_s,
            "transactions_started": self.transactions_started,
            "transactions_committed": self.transactions_committed,
            "transactions_aborted": self.transactions_aborted,
            "transactions_incomplete": self.transactions_incomplete,
            "completed_transactions": self.completed_transactions,
            "executed_operations": self.executed_operations,
            "reads_executed": self.reads_executed,
            "updates_executed": self.updates_executed,
            "tx_throughput_tps": self.tx_throughput_tps,
            "committed_tps": self.committed_tps,
            "op_throughput_ops": self.op_throughput_ops,
        }


@dataclass(frozen=True)
class RecoveryRunMetrics:
    """Metrics captured during the timed crash recovery interval."""

    total_duration_s: float
    analysis_duration_s: Optional[float]
    redo_duration_s: Optional[float]
    undo_duration_s: Optional[float]
    records_redone: int
    clrs_generated: int
    updates_undone: int
    pages_modified_count: int
    winners_count: int
    losers_count: int

    @classmethod
    def create(
        cls,
        total_duration_s: float,
        analysis_duration_s: Optional[float] = None,
        redo_duration_s: Optional[float] = None,
        undo_duration_s: Optional[float] = None,
        records_redone: int = 0,
        clrs_generated: int = 0,
        updates_undone: int = 0,
        pages_modified_count: int = 0,
        winners_count: int = 0,
        losers_count: int = 0,
    ) -> "RecoveryRunMetrics":
        """Construct RecoveryRunMetrics with safe bounds."""
        safe_total = max(0.0, float(total_duration_s))
        safe_analysis = max(0.0, float(analysis_duration_s)) if analysis_duration_s is not None else None
        safe_redo = max(0.0, float(redo_duration_s)) if redo_duration_s is not None else None
        safe_undo = max(0.0, float(undo_duration_s)) if undo_duration_s is not None else None

        return cls(
            total_duration_s=safe_total,
            analysis_duration_s=safe_analysis,
            redo_duration_s=safe_redo,
            undo_duration_s=safe_undo,
            records_redone=records_redone,
            clrs_generated=clrs_generated,
            updates_undone=updates_undone,
            pages_modified_count=pages_modified_count,
            winners_count=winners_count,
            losers_count=losers_count,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert recovery metrics to dictionary representation."""
        return {
            "total_duration_s": self.total_duration_s,
            "analysis_duration_s": self.analysis_duration_s,
            "redo_duration_s": self.redo_duration_s,
            "undo_duration_s": self.undo_duration_s,
            "records_redone": self.records_redone,
            "clrs_generated": self.clrs_generated,
            "updates_undone": self.updates_undone,
            "pages_modified_count": self.pages_modified_count,
            "winners_count": self.winners_count,
            "losers_count": self.losers_count,
        }


@dataclass(frozen=True)
class BenchmarkRunResult:
    """Outcome and captured timings of a single benchmark run."""

    run_index: int
    is_warmup: bool
    is_success: bool
    validation_passed: bool
    validation_errors: List[str]
    error_message: Optional[str]
    workload_metrics: Optional[WorkloadRunMetrics]
    recovery_metrics: Optional[RecoveryRunMetrics]

    def to_dict(self) -> Dict[str, Any]:
        """Convert run result to dictionary representation."""
        return {
            "run_index": self.run_index,
            "is_warmup": self.is_warmup,
            "is_success": self.is_success,
            "validation_passed": self.validation_passed,
            "validation_errors": list(self.validation_errors),
            "error_message": self.error_message,
            "workload": self.workload_metrics.to_dict() if self.workload_metrics is not None else None,
            "recovery": self.recovery_metrics.to_dict() if self.recovery_metrics is not None else None,
        }


@dataclass(frozen=True)
class MetricDistribution:
    """Descriptive statistics for a numeric measurement across runs."""

    count: int
    mean: float
    median: float
    min: float
    max: float
    std_dev: float
    p95: float

    @classmethod
    def from_values(cls, values: List[float]) -> "MetricDistribution":
        """Compute distribution statistics from a list of floats."""
        if not values:
            return cls(count=0, mean=0.0, median=0.0, min=0.0, max=0.0, std_dev=0.0, p95=0.0)

        sorted_vals = sorted(values)
        count = len(sorted_vals)
        mean_val = statistics.mean(sorted_vals)
        median_val = statistics.median(sorted_vals)
        min_val = sorted_vals[0]
        max_val = sorted_vals[-1]
        std_val = statistics.stdev(sorted_vals) if count > 1 else 0.0

        # Percentile 95 calculation using linear interpolation
        # Note: for small sample sizes (N < 20), p95 is an interpolated estimate
        if count == 1:
            p95_val = sorted_vals[0]
        else:
            fraction = 0.95 * (count - 1)
            lower_idx = int(fraction)
            upper_idx = min(count - 1, lower_idx + 1)
            weight = fraction - lower_idx
            p95_val = sorted_vals[lower_idx] * (1.0 - weight) + sorted_vals[upper_idx] * weight

        return cls(
            count=count,
            mean=round(mean_val, 4),
            median=round(median_val, 4),
            min=round(min_val, 4),
            max=round(max_val, 4),
            std_dev=round(std_val, 4),
            p95=round(p95_val, 4),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert distribution statistics to dictionary representation."""
        return {
            "count": self.count,
            "mean": self.mean,
            "median": self.median,
            "min": self.min,
            "max": self.max,
            "std_dev": self.std_dev,
            "p95": self.p95,
        }


class BenchmarkScenarioResult:
    """Aggregate result of an entire benchmark scenario across all runs."""

    def __init__(
        self,
        config: BenchmarkConfig,
        runs: List[BenchmarkRunResult],
        environment: Optional[EnvironmentMetadata] = None,
    ) -> None:
        self._config: BenchmarkConfig = config
        self._runs: List[BenchmarkRunResult] = list(runs)
        self._environment: Optional[EnvironmentMetadata] = environment

        self._warmup_runs: List[BenchmarkRunResult] = [r for r in self._runs if r.is_warmup]
        self._measured_runs: List[BenchmarkRunResult] = [r for r in self._runs if not r.is_warmup]

        self._valid_runs: List[BenchmarkRunResult] = [
            r for r in self._measured_runs if r.is_success and r.validation_passed
        ]
        self._failed_runs: List[BenchmarkRunResult] = [
            r for r in self._measured_runs if not (r.is_success and r.validation_passed)
        ]

        # Calculate distributions for valid measured runs
        self._aggregates: Dict[str, MetricDistribution] = self._compute_aggregates()

    @property
    def config(self) -> BenchmarkConfig:
        return self._config

    @property
    def environment(self) -> Optional[EnvironmentMetadata]:
        return self._environment

    @property
    def all_runs(self) -> List[BenchmarkRunResult]:
        return list(self._runs)

    @property
    def warmup_runs(self) -> List[BenchmarkRunResult]:
        return list(self._warmup_runs)

    @property
    def measured_runs(self) -> List[BenchmarkRunResult]:
        return list(self._measured_runs)

    @property
    def valid_runs(self) -> List[BenchmarkRunResult]:
        return list(self._valid_runs)

    @property
    def failed_runs(self) -> List[BenchmarkRunResult]:
        return list(self._failed_runs)

    @property
    def is_success(self) -> bool:
        """True if all measured repetitions passed validation successfully."""
        return len(self._measured_runs) > 0 and len(self._failed_runs) == 0

    @property
    def aggregates(self) -> Dict[str, MetricDistribution]:
        return dict(self._aggregates)

    def _compute_aggregates(self) -> Dict[str, MetricDistribution]:
        """Compute statistical distributions across successful measured runs."""
        workload_durations: List[float] = []
        tx_tps: List[float] = []
        op_ops: List[float] = []

        recovery_durations: List[float] = []
        analysis_durations: List[float] = []
        redo_durations: List[float] = []
        undo_durations: List[float] = []

        for r in self._valid_runs:
            if r.workload_metrics is not None:
                workload_durations.append(r.workload_metrics.duration_s)
                tx_tps.append(r.workload_metrics.tx_throughput_tps)
                op_ops.append(r.workload_metrics.op_throughput_ops)
            if r.recovery_metrics is not None:
                recovery_durations.append(r.recovery_metrics.total_duration_s)
                if r.recovery_metrics.analysis_duration_s is not None:
                    analysis_durations.append(r.recovery_metrics.analysis_duration_s)
                if r.recovery_metrics.redo_duration_s is not None:
                    redo_durations.append(r.recovery_metrics.redo_duration_s)
                if r.recovery_metrics.undo_duration_s is not None:
                    undo_durations.append(r.recovery_metrics.undo_duration_s)

        res: Dict[str, MetricDistribution] = {}
        if workload_durations:
            res["workload_duration_s"] = MetricDistribution.from_values(workload_durations)
            res["tx_throughput_tps"] = MetricDistribution.from_values(tx_tps)
            res["op_throughput_ops"] = MetricDistribution.from_values(op_ops)

        if recovery_durations:
            res["recovery_duration_s"] = MetricDistribution.from_values(recovery_durations)
        if analysis_durations:
            res["analysis_duration_s"] = MetricDistribution.from_values(analysis_durations)
        if redo_durations:
            res["redo_duration_s"] = MetricDistribution.from_values(redo_durations)
        if undo_durations:
            res["undo_duration_s"] = MetricDistribution.from_values(undo_durations)

        return res

    def to_dict(self) -> Dict[str, Any]:
        """Convert scenario result to dictionary representation."""
        return {
            "config": self._config.to_dict(),
            "environment": self._environment.to_dict() if self._environment is not None else None,
            "is_success": self.is_success,
            "total_runs": len(self._runs),
            "warmup_runs_count": len(self._warmup_runs),
            "measured_runs_count": len(self._measured_runs),
            "valid_runs_count": len(self._valid_runs),
            "failed_runs_count": len(self._failed_runs),
            "aggregates": {k: v.to_dict() for k, v in self._aggregates.items()},
            "measured_runs": [r.to_dict() for r in self._measured_runs],
            "warmup_runs": [r.to_dict() for r in self._warmup_runs],
        }

    def __repr__(self) -> str:
        status = "PASSED" if self.is_success else "FAILED"
        return (
            f"BenchmarkScenarioResult(scenario='{self._config.scenario_name}', "
            f"status={status}, valid={len(self._valid_runs)}/{len(self._measured_runs)})"
        )
