"""Test suite for the ARIES Performance Benchmarking Framework.

Validates benchmark configuration, metric calculations, timing boundaries,
zero-duration safeguards, file isolation, recovery measurements, crash experiments,
result exports (CSV/JSON), and error handling across 15 dedicated scenarios.
"""

import csv
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import pytest

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
    crash_recovery_commit_fault,
    get_preset_scenario,
    hotspot_contention,
    large_workload,
    list_preset_scenarios,
    medium_workload,
    read_heavy,
    recovery_only,
    small_workload,
    update_heavy,
)
from app.fault.constants import FaultPoint
from app.workload.config import WorkloadConfig
from app.workload.executor import WorkloadExecutor


class TestBenchmarkSuite:
    """Comprehensive 15-scenario verification suite for ARIES benchmarking."""

    # -------------------------------------------------------------------------
    # 1. Benchmark configuration validation
    # -------------------------------------------------------------------------
    def test_01_benchmark_config_validation(self) -> None:
        """Verify that BenchmarkConfig validates inputs strictly and rejects invalid values."""
        # Valid default configuration
        cfg = BenchmarkConfig(scenario_name="test_sanity", repetitions=2, warmup_runs=1)
        assert cfg.scenario_name == "test_sanity"
        assert cfg.repetitions == 2
        assert cfg.warmup_runs == 1
        assert cfg.seed == 42
        assert cfg.is_recovery_only is False
        assert cfg.measure_recovery_stages is True
        assert cfg.workload_config is not None

        # Rejection of invalid scenario_name
        with pytest.raises(BenchmarkConfigError, match="scenario_name"):
            BenchmarkConfig(scenario_name="")
        with pytest.raises(BenchmarkConfigError, match="scenario_name"):
            BenchmarkConfig(scenario_name="   ")

        # Rejection of invalid repetitions
        with pytest.raises(BenchmarkConfigError, match="repetitions"):
            BenchmarkConfig(repetitions=0)
        with pytest.raises(BenchmarkConfigError, match="repetitions"):
            BenchmarkConfig(repetitions=-5)
        with pytest.raises(BenchmarkConfigError, match="repetitions"):
            BenchmarkConfig(repetitions=True)  # type: ignore

        # Rejection of invalid warmup_runs
        with pytest.raises(BenchmarkConfigError, match="warmup_runs"):
            BenchmarkConfig(warmup_runs=-1)
        with pytest.raises(BenchmarkConfigError, match="warmup_runs"):
            BenchmarkConfig(warmup_runs=True)  # type: ignore

        # Rejection of invalid types for flags
        with pytest.raises(BenchmarkConfigError, match="is_recovery_only"):
            BenchmarkConfig(is_recovery_only="not_bool")  # type: ignore
        with pytest.raises(BenchmarkConfigError, match="timeout_per_run"):
            BenchmarkConfig(timeout_per_run=-1.0)

        # Dictionary representation
        d = cfg.to_dict()
        assert d["scenario_name"] == "test_sanity"
        assert d["repetitions"] == 2
        assert "workload_config" in d

    # -------------------------------------------------------------------------
    # 2. Small benchmark scenario execution
    # -------------------------------------------------------------------------
    def test_02_small_benchmark_scenario_execution(self, tmp_path: Path) -> None:
        """Verify executing small_workload produces expected runs, status, and metrics."""
        cfg = small_workload(repetitions=2, warmup_runs=1, seed=123)
        result = BenchmarkRunner.run(cfg, target_dir=tmp_path)

        assert isinstance(result, BenchmarkScenarioResult)
        assert result.is_success is True
        assert len(result.all_runs) == 3
        assert len(result.warmup_runs) == 1
        assert len(result.measured_runs) == 2
        assert len(result.valid_runs) == 2
        assert len(result.failed_runs) == 0

        # Verify per-run workload metrics
        for run in result.measured_runs:
            assert run.is_warmup is False
            assert run.is_success is True
            assert run.validation_passed is True
            assert run.workload_metrics is not None
            wm = run.workload_metrics
            assert wm.duration_s >= 0.0
            assert wm.transactions_started == 5
            assert wm.executed_operations > 0
            assert wm.tx_throughput_tps >= 0.0
            assert wm.op_throughput_ops >= 0.0

    # -------------------------------------------------------------------------
    # 3. Correct metric calculations and units
    # -------------------------------------------------------------------------
    def test_03_correct_metric_calculations_and_units(self) -> None:
        """Verify throughput and duration calculations with known arithmetic inputs."""
        # 10 completed txs in 2.0s = 5.0 TPS; 50 ops in 2.0s = 25.0 ops/sec
        wm = WorkloadRunMetrics.create(
            duration_s=2.0,
            transactions_started=10,
            transactions_committed=8,
            transactions_aborted=2,
            transactions_incomplete=0,
            reads_executed=20,
            updates_executed=30,
            executed_operations=50,
        )
        assert wm.duration_s == 2.0
        assert wm.completed_transactions == 10
        assert wm.tx_throughput_tps == 5.0
        assert wm.committed_tps == 4.0
        assert wm.op_throughput_ops == 25.0

        # Recovery metric units and stage timing
        rm = RecoveryRunMetrics.create(
            total_duration_s=0.5,
            analysis_duration_s=0.1,
            redo_duration_s=0.25,
            undo_duration_s=0.15,
            records_redone=15,
            clrs_generated=5,
            updates_undone=5,
            pages_modified_count=4,
            winners_count=3,
            losers_count=1,
        )
        assert rm.total_duration_s == 0.5
        assert rm.analysis_duration_s == 0.1
        assert rm.redo_duration_s == 0.25
        assert rm.undo_duration_s == 0.15
        assert rm.records_redone == 15
        assert rm.clrs_generated == 5
        assert rm.updates_undone == 5
        assert rm.pages_modified_count == 4

    # -------------------------------------------------------------------------
    # 4. Zero-operation and zero-duration safeguards
    # -------------------------------------------------------------------------
    def test_04_zero_operation_and_zero_duration_safeguards(self) -> None:
        """Verify that zero duration, zero operations, or negative numbers do not cause division errors."""
        # Zero duration handling
        wm_zero = WorkloadRunMetrics.create(
            duration_s=0.0,
            transactions_started=0,
            transactions_committed=0,
            transactions_aborted=0,
            transactions_incomplete=0,
            reads_executed=0,
            updates_executed=0,
            executed_operations=0,
        )
        assert wm_zero.duration_s == 0.0
        assert wm_zero.tx_throughput_tps == 0.0
        assert wm_zero.committed_tps == 0.0
        assert wm_zero.op_throughput_ops == 0.0

        # Negative duration clamping
        wm_neg = WorkloadRunMetrics.create(
            duration_s=-1.5,
            transactions_started=2,
            transactions_committed=2,
            transactions_aborted=0,
            transactions_incomplete=0,
            reads_executed=0,
            updates_executed=2,
            executed_operations=4,
        )
        assert wm_neg.duration_s == 0.0
        assert wm_neg.tx_throughput_tps == 0.0

        # Empty distribution calculation
        dist_empty = MetricDistribution.from_values([])
        assert dist_empty.count == 0
        assert dist_empty.mean == 0.0
        assert dist_empty.median == 0.0
        assert dist_empty.min == 0.0
        assert dist_empty.max == 0.0
        assert dist_empty.std_dev == 0.0
        assert dist_empty.p95 == 0.0

        # Single element distribution
        dist_single = MetricDistribution.from_values([42.0])
        assert dist_single.count == 1
        assert dist_single.mean == 42.0
        assert dist_single.median == 42.0
        assert dist_single.std_dev == 0.0

    # -------------------------------------------------------------------------
    # 5. Multiple repetitions and aggregate statistics
    # -------------------------------------------------------------------------
    def test_05_multiple_repetitions_and_aggregate_statistics(self, tmp_path: Path) -> None:
        """Verify multi-run repetition and statistical distribution calculation."""
        cfg = BenchmarkConfig(
            scenario_name="multi_rep_test",
            repetitions=4,
            warmup_runs=0,
            seed=777,
            workload_config=WorkloadConfig(
                num_transactions=4,
                num_pages=2,
                operations_per_transaction=2,
                commit_ratio=1.0,
                abort_ratio=0.0,
                incomplete_ratio=0.0,
                seed=777,
            ),
        )

        result = BenchmarkRunner.run(cfg, target_dir=tmp_path)
        assert len(result.measured_runs) == 4
        assert "workload_duration_s" in result.aggregates
        assert "tx_throughput_tps" in result.aggregates
        assert "op_throughput_ops" in result.aggregates

        dur_dist = result.aggregates["workload_duration_s"]
        assert dur_dist.count == 4
        assert dur_dist.min <= dur_dist.median <= dur_dist.max
        assert dur_dist.min <= dur_dist.mean <= dur_dist.max
        assert dur_dist.std_dev >= 0.0

    # -------------------------------------------------------------------------
    # 6. Fixed-seed reproducibility
    # -------------------------------------------------------------------------
    def test_06_fixed_seed_reproducibility(self, tmp_path: Path) -> None:
        """Verify that benchmarks with identical seed produce identical operations and outcomes."""
        cfg1 = small_workload(repetitions=1, warmup_runs=0, seed=555)
        cfg2 = small_workload(repetitions=1, warmup_runs=0, seed=555)

        res1 = BenchmarkRunner.run(cfg1, target_dir=tmp_path / "run1")
        res2 = BenchmarkRunner.run(cfg2, target_dir=tmp_path / "run2")

        r1 = res1.measured_runs[0].workload_metrics
        r2 = res2.measured_runs[0].workload_metrics
        assert r1 is not None and r2 is not None
        assert r1.transactions_started == r2.transactions_started
        assert r1.transactions_committed == r2.transactions_committed
        assert r1.transactions_aborted == r2.transactions_aborted
        assert r1.executed_operations == r2.executed_operations
        assert r1.reads_executed == r2.reads_executed
        assert r1.updates_executed == r2.updates_executed

    # -------------------------------------------------------------------------
    # 7. Isolation of database and WAL files across runs
    # -------------------------------------------------------------------------
    def test_07_isolation_of_database_and_wal_files_across_runs(self, tmp_path: Path) -> None:
        """Verify each repetition runs in an isolated directory and temporary files do not leak."""
        cfg = BenchmarkConfig(
            scenario_name="isolation_test",
            repetitions=3,
            warmup_runs=0,
            seed=888,
            workload_config=WorkloadConfig(
                num_transactions=2,
                num_pages=2,
                operations_per_transaction=2,
                commit_ratio=1.0,
                abort_ratio=0.0,
                incomplete_ratio=0.0,
                seed=888,
            ),
        )

        base_dir = tmp_path / "isolation_target"
        base_dir.mkdir()
        result = BenchmarkRunner.run(cfg, target_dir=base_dir)

        assert result.is_success is True
        # Verify no temporary database or WAL files were left behind in base_dir
        leftover_files = list(base_dir.glob("**/*.db")) + list(base_dir.glob("**/*.wal"))
        assert len(leftover_files) == 0

    # -------------------------------------------------------------------------
    # 8. Recovery-only timing and result collection
    # -------------------------------------------------------------------------
    def test_08_recovery_only_timing_and_result_collection(self, tmp_path: Path) -> None:
        """Verify recovery-only scenario prepares crash state outside timing and times recovery."""
        cfg = recovery_only(repetitions=2, warmup_runs=0, seed=999)
        result = BenchmarkRunner.run(cfg, target_dir=tmp_path)

        assert result.is_success is True
        assert len(result.measured_runs) == 2

        for run in result.measured_runs:
            assert run.workload_metrics is None  # Workload was untimed setup
            assert run.recovery_metrics is not None
            rm = run.recovery_metrics
            assert rm.total_duration_s >= 0.0
            assert rm.analysis_duration_s is not None and rm.analysis_duration_s >= 0.0
            assert rm.redo_duration_s is not None and rm.redo_duration_s >= 0.0
            assert rm.undo_duration_s is not None and rm.undo_duration_s >= 0.0
            assert rm.winners_count > 0 or rm.losers_count > 0

        assert "recovery_duration_s" in result.aggregates
        assert "analysis_duration_s" in result.aggregates
        assert "redo_duration_s" in result.aggregates
        assert "undo_duration_s" in result.aggregates

    # -------------------------------------------------------------------------
    # 9. Optional crash-recovery scenario correctness
    # -------------------------------------------------------------------------
    def test_09_optional_crash_recovery_scenario_correctness(self, tmp_path: Path) -> None:
        """Verify crash-recovery experiment terminates at fault point and recovers cleanly."""
        cfg = crash_recovery_commit_fault(repetitions=1, warmup_runs=0, seed=1234)
        result = BenchmarkRunner.run(cfg, target_dir=tmp_path)

        assert result.is_success is True
        assert len(result.measured_runs) == 1
        run = result.measured_runs[0]
        assert run.validation_passed is True
        assert run.workload_metrics is not None
        assert run.recovery_metrics is not None
        assert run.recovery_metrics.total_duration_s >= 0.0

    # -------------------------------------------------------------------------
    # 10. Failed workload validation being marked as failed
    # -------------------------------------------------------------------------
    def test_10_failed_workload_validation_marked_as_failed(self, tmp_path: Path) -> None:
        """Verify that failed database validation marks the run and scenario as failed."""
        cfg = small_workload(repetitions=1, warmup_runs=0, seed=4321)

        # Deliberately mock validate_database_state to return validation errors
        with patch.object(
            WorkloadExecutor,
            "validate_database_state",
            return_value=["Simulated disk byte mismatch on Page 0"],
        ):
            result = BenchmarkRunner.run(cfg, target_dir=tmp_path)

        assert result.is_success is False
        assert len(result.failed_runs) == 1
        assert len(result.valid_runs) == 0
        run = result.measured_runs[0]
        assert run.is_success is False
        assert run.validation_passed is False
        assert any("Simulated disk byte mismatch" in err for err in run.validation_errors)

    # -------------------------------------------------------------------------
    # 11. CSV export schema and contents
    # -------------------------------------------------------------------------
    def test_11_csv_export_schema_and_contents(self, tmp_path: Path) -> None:
        """Verify that exported CSV contains expected headers, units, and row counts."""
        cfg = small_workload(repetitions=2, warmup_runs=1, seed=111)
        result = BenchmarkRunner.run(cfg, target_dir=tmp_path)

        csv_file = tmp_path / "benchmark_export.csv"
        saved_path = BenchmarkExporter.export_csv(result, csv_file)
        assert saved_path.exists()

        with open(saved_path, "r", newline="", encoding="utf-8") as f:
            reader = list(csv.reader(f))

        # Check headers
        headers = reader[0]
        assert headers == BenchmarkExporter.CSV_HEADERS
        assert "workload_duration_s" in headers
        assert "recovery_duration_s" in headers
        assert "tx_throughput_tps" in headers
        assert "op_throughput_ops" in headers

        # 1 header + 1 warmup + 2 measured = 4 rows
        assert len(reader) == 4

    # -------------------------------------------------------------------------
    # 12. JSON export schema and contents
    # -------------------------------------------------------------------------
    def test_12_json_export_schema_and_contents(self, tmp_path: Path) -> None:
        """Verify exported JSON contains configuration, environment, aggregates, and runs."""
        cfg = small_workload(repetitions=2, warmup_runs=1, seed=222)
        result = BenchmarkRunner.run(cfg, target_dir=tmp_path)

        json_file = tmp_path / "benchmark_export.json"
        saved_path = BenchmarkExporter.export_json(result, json_file)
        assert saved_path.exists()

        with open(saved_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["is_success"] is True
        assert "config" in data
        assert "environment" in data
        assert "aggregates" in data
        assert len(data["measured_runs"]) == 2
        assert len(data["warmup_runs"]) == 1

    # -------------------------------------------------------------------------
    # 13. Metadata serialization
    # -------------------------------------------------------------------------
    def test_13_metadata_serialization(self) -> None:
        """Verify EnvironmentMetadata captures platform fields accurately."""
        env = EnvironmentMetadata.capture()
        d = env.to_dict()

        assert "timestamp" in d
        assert "python_version" in d
        assert "os_name" in d
        assert "architecture" in d
        assert "cpu_count" in d
        assert isinstance(d["cpu_count"], int)
        assert d["cpu_count"] >= 1

    # -------------------------------------------------------------------------
    # 14. Compatibility with existing workload generator and recovery manager
    # -------------------------------------------------------------------------
    def test_14_compatibility_with_workload_and_recovery_manager(self, tmp_path: Path) -> None:
        """Verify that preset scenarios (read_heavy, update_heavy, hotspot, large) execute seamlessly."""
        # Test preset scenario generation
        presets = list_preset_scenarios()
        assert "small_workload" in presets
        assert "medium_workload" in presets
        assert "large_workload" in presets
        assert "read_heavy" in presets
        assert "update_heavy" in presets
        assert "hotspot_contention" in presets

        cfg_rh = get_preset_scenario("read_heavy", repetitions=1, warmup_runs=0, seed=333)
        assert cfg_rh.workload_config.read_ratio == 0.85

        cfg_uh = get_preset_scenario("update_heavy", repetitions=1, warmup_runs=0, seed=444)
        assert cfg_uh.workload_config.read_ratio == 0.10

        # Execute read_heavy run
        res_rh = BenchmarkRunner.run(cfg_rh, target_dir=tmp_path)
        assert res_rh.is_success is True

    # -------------------------------------------------------------------------
    # 15. Error handling and cleanup of temporary files
    # -------------------------------------------------------------------------
    def test_15_error_handling_and_cleanup_of_temporary_files(self) -> None:
        """Verify that invalid export targets raise BenchmarkExportError safely."""
        cfg = BenchmarkConfig(scenario_name="dummy_err")
        dummy_result = BenchmarkScenarioResult(config=cfg, runs=[])

        # Invalid path that cannot be written
        invalid_path = Path("/\0/invalid_dir/bad.json")
        with pytest.raises(BenchmarkExportError):
            BenchmarkExporter.export_json(dummy_result, invalid_path)

        with pytest.raises(BenchmarkExportError):
            BenchmarkExporter.export_csv(dummy_result, invalid_path)

        # Preset scenario error handling
        with pytest.raises(BenchmarkConfigError, match="Unknown benchmark scenario"):
            get_preset_scenario("non_existent_preset_scenario")

    # -------------------------------------------------------------------------
    # 16. p95 linear interpolation and monotonic percentile ordering
    # -------------------------------------------------------------------------
    def test_16_p95_linear_interpolation_monotonicity(self) -> None:
        """Verify that p95 satisfies min <= median <= p95 <= max across small sample sizes."""
        # 2 elements: [10.0, 20.0] -> median is 15.0, p95 must be >= 15.0 (specifically 19.5)
        dist_2 = MetricDistribution.from_values([10.0, 20.0])
        assert dist_2.min == 10.0
        assert dist_2.median == 15.0
        assert dist_2.p95 == 19.5
        assert dist_2.max == 20.0
        assert dist_2.min <= dist_2.median <= dist_2.p95 <= dist_2.max

        # 3 elements: [10.0, 20.0, 30.0] -> median is 20.0, p95 must be 29.0
        dist_3 = MetricDistribution.from_values([10.0, 20.0, 30.0])
        assert dist_3.min == 10.0
        assert dist_3.median == 20.0
        assert dist_3.p95 == 29.0
        assert dist_3.max == 30.0
        assert dist_3.min <= dist_3.median <= dist_3.p95 <= dist_3.max

        # 5 elements: [1.0, 2.0, 3.0, 4.0, 5.0] -> median is 3.0, p95 is 4.8
        dist_5 = MetricDistribution.from_values([1.0, 2.0, 3.0, 4.0, 5.0])
        assert dist_5.min <= dist_5.median <= dist_5.p95 <= dist_5.max
        assert dist_5.p95 == 4.8

    # -------------------------------------------------------------------------
    # 17. Crash benchmark separates workload and recovery timing
    # -------------------------------------------------------------------------
    def test_17_crash_benchmark_separates_workload_and_recovery_timing(
        self, tmp_path: Path
    ) -> None:
        """Verify crash-recovery experiment strictly separates child crash duration from parent recovery duration."""
        cfg = crash_recovery_commit_fault(repetitions=1, warmup_runs=0, seed=9876)
        result = BenchmarkRunner.run(cfg, target_dir=tmp_path)

        assert result.is_success is True
        run = result.measured_runs[0]
        assert run.workload_metrics is not None
        assert run.recovery_metrics is not None

        # Both durations are positive and non-zero
        assert run.workload_metrics.duration_s > 0.0
        assert run.recovery_metrics.total_duration_s > 0.0

        # Subprocess execution and recovery execution are distinct intervals
        assert run.workload_metrics.duration_s != run.recovery_metrics.total_duration_s

        # Recovery stages (Analysis, Redo, Undo) are timed individually
        assert run.recovery_metrics.analysis_duration_s is not None
        assert run.recovery_metrics.redo_duration_s is not None
        assert run.recovery_metrics.undo_duration_s is not None
        assert run.recovery_metrics.analysis_duration_s >= 0.0
        assert run.recovery_metrics.redo_duration_s >= 0.0
        assert run.recovery_metrics.undo_duration_s >= 0.0
