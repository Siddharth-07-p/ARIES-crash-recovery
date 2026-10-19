"""ARIES Performance Benchmarking Framework.

Provides modular benchmark configuration, execution, timing measurements,
metrics aggregation, and JSON/CSV export facilities for the ARIES database engine.
"""

from app.benchmark.config import BenchmarkConfig
from app.benchmark.exceptions import (
    BenchmarkConfigError,
    BenchmarkError,
    BenchmarkExecutionError,
    BenchmarkExportError,
)
from app.benchmark.exporter import BenchmarkExporter
from app.benchmark.models import (
    BenchmarkRunResult,
    BenchmarkScenarioResult,
    EnvironmentMetadata,
    MetricDistribution,
    RecoveryRunMetrics,
    WorkloadRunMetrics,
)
from app.benchmark.runner import BenchmarkRunner
from app.benchmark.scenarios import (
    get_preset_scenario,
    list_preset_scenarios,
    small_workload,
    medium_workload,
    large_workload,
    read_heavy,
    update_heavy,
    hotspot_contention,
    interleaved_concurrent,
    high_abort,
    recovery_only,
    crash_recovery_commit_fault,
)

__all__ = [
    "BenchmarkConfig",
    "BenchmarkError",
    "BenchmarkConfigError",
    "BenchmarkExecutionError",
    "BenchmarkExportError",
    "BenchmarkExporter",
    "BenchmarkRunResult",
    "BenchmarkScenarioResult",
    "BenchmarkRunner",
    "EnvironmentMetadata",
    "MetricDistribution",
    "WorkloadRunMetrics",
    "RecoveryRunMetrics",
    "get_preset_scenario",
    "list_preset_scenarios",
    "small_workload",
    "medium_workload",
    "large_workload",
    "read_heavy",
    "update_heavy",
    "hotspot_contention",
    "interleaved_concurrent",
    "high_abort",
    "recovery_only",
    "crash_recovery_commit_fault",
]
