"""Tests for Phase 16C: Benchmark-to-Monitoring Integration.

Validates:
1. Successful benchmark execution through POST /api/benchmark/run.
2. Result registration and retrieval via GET /api/monitoring/benchmark.
3. Scenario discovery endpoint GET /api/benchmark/scenarios.
4. Drop-in alias endpoint POST /api/benchmark/execute.
5. Strict validation of scenario names, repetitions, warmup runs, and timeouts (422).
6. Safe rejection with 503 when live database is uninitialized or closed.
7. Concurrency protection returning 409 Conflict when a benchmark or workload is active.
8. Mutual exclusion between live workload execution and benchmark execution.
9. Storage safety: live database and WAL files are never overwritten, truncated, or corrupted.
10. Benchmark validation failure handling: 422 returned and no false success registered.
11. Internal execution failure handling: 500 returned without exposing stack traces.
12. Recovery scenario execution capturing recovery metrics (mean_recovery_duration_s).
"""

from pathlib import Path
from typing import Any, Dict
import pytest
from fastapi.testclient import TestClient

from app.benchmark.exceptions import BenchmarkExecutionError
from app.benchmark.models import (
    BenchmarkRunResult,
    BenchmarkScenarioResult,
    MetricDistribution,
    WorkloadRunMetrics,
)
from app.benchmark.router import BenchmarkRunRequest, BenchmarkRunResponse
from app.benchmark.runner import BenchmarkRunner
from app.main import app
from app.monitoring.models import BenchmarkSummaryResponse
from app.monitoring.state import monitoring_state
from app.workload.router import workload_lock


@pytest.fixture(autouse=True)
def reset_monitoring_and_lock():
    """Reset monitoring state and ensure concurrency lock is free between tests."""
    monitoring_state.reset()
    if workload_lock.locked():
        workload_lock.release()
    yield
    monitoring_state.reset()
    if workload_lock.locked():
        workload_lock.release()


class TestBenchmarkAPI:
    """Comprehensive test suite for Phase 16C Benchmark Integration API."""

    def test_01_successful_benchmark_execution(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify successful benchmark execution and valid response structure."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            payload = {
                "scenario": "small_workload",
                "repetitions": 2,
                "warmup_runs": 1,
                "seed": 42,
            }
            resp = client.post("/api/benchmark/run", json=payload)
            assert resp.status_code == 200, resp.text
            data = resp.json()

            validated = BenchmarkRunResponse.model_validate(data)
            assert validated.success is True
            assert validated.is_success is True
            assert validated.scenario_name == "small_workload"
            assert validated.execution_mode == "isolated_temporary_storage"
            assert "isolated" in validated.isolation_note.lower()
            assert validated.total_runs == 3
            assert validated.measured_runs_count == 2
            assert validated.warmup_runs_count == 1
            assert validated.valid_runs_count == 2
            assert validated.failed_runs_count == 0
            assert validated.mean_tx_throughput_tps is not None and validated.mean_tx_throughput_tps > 0.0
            assert validated.mean_op_throughput_ops is not None and validated.mean_op_throughput_ops > 0.0
            assert validated.mean_workload_duration_s is not None and validated.mean_workload_duration_s > 0.0
            assert validated.duration_seconds > 0.0
            assert "tx_throughput_tps" in validated.aggregates

            # Subsystem managers in app.state remain open and functional
            assert not app.state.disk_manager.is_closed
            assert not app.state.log_manager.is_closed
            assert not app.state.buffer_manager.is_closed

    def test_02_benchmark_result_registered_and_retrievable_via_monitoring(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify benchmark outcome is registered with monitoring_state and visible on monitoring endpoints."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Initially benchmark summary is not available
            pre_resp = client.get("/api/monitoring/benchmark")
            assert pre_resp.status_code == 200
            pre_data = BenchmarkSummaryResponse.model_validate(pre_resp.json())
            assert pre_data.status == "not_available"

            # Execute benchmark
            payload = {
                "scenario": "small_workload",
                "repetitions": 2,
                "warmup_runs": 1,
                "seed": 123,
            }
            exec_resp = client.post("/api/benchmark/run", json=payload)
            assert exec_resp.status_code == 200
            run_data = exec_resp.json()

            # Monitoring endpoint now reports 'available' with matching metrics
            post_resp = client.get("/api/monitoring/benchmark")
            assert post_resp.status_code == 200
            post_data = BenchmarkSummaryResponse.model_validate(post_resp.json())
            assert post_data.status == "available"
            assert post_data.scenario_name == "small_workload"
            assert post_data.is_success is True
            assert post_data.total_runs == 3
            assert post_data.measured_runs_count == 2
            assert post_data.warmup_runs_count == 1
            assert post_data.valid_runs_count == 2
            assert post_data.failed_runs_count == 0
            assert post_data.mean_tx_throughput_tps == run_data["mean_tx_throughput_tps"]
            assert post_data.mean_op_throughput_ops == run_data["mean_op_throughput_ops"]
            assert post_data.mean_workload_duration_s == run_data["mean_workload_duration_s"]

    def test_03_benchmark_scenarios_discovery_endpoint(self) -> None:
        """Verify GET /api/benchmark/scenarios returns list of available presets and limits."""
        with TestClient(app) as client:
            resp = client.get("/api/benchmark/scenarios")
            assert resp.status_code == 200
            data = resp.json()
            assert "scenarios" in data
            assert "small_workload" in data["scenarios"]
            assert "read_heavy" in data["scenarios"]
            assert "recovery_only" in data["scenarios"]
            assert data["default_scenario"] == "small_workload"
            assert data["limits"]["max_repetitions"] == 10

    def test_04_benchmark_execute_alias_endpoint(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify POST /api/benchmark/execute acts as a drop-in alias for /run."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            resp = client.post(
                "/api/benchmark/execute",
                json={
                    "scenario": "small_workload",
                    "repetitions": 1,
                    "warmup_runs": 0,
                    "seed": 999,
                },
            )
            assert resp.status_code == 200
            assert resp.json()["success"] is True
            assert resp.json()["measured_runs_count"] == 1

    def test_05_invalid_parameters_bounds_rejected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify bounds checking and 422 responses for invalid scenarios and parameters."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Unknown scenario
            r1 = client.post("/api/benchmark/run", json={"scenario": "nonexistent_scenario"})
            assert r1.status_code == 422
            assert "Unknown scenario" in r1.text

            # repetitions < 1
            r2 = client.post("/api/benchmark/run", json={"repetitions": 0})
            assert r2.status_code == 422

            # repetitions > 10
            r3 = client.post("/api/benchmark/run", json={"repetitions": 11})
            assert r3.status_code == 422

            # warmup_runs < 0
            r4 = client.post("/api/benchmark/run", json={"warmup_runs": -1})
            assert r4.status_code == 422

            # warmup_runs > 5
            r5 = client.post("/api/benchmark/run", json={"warmup_runs": 6})
            assert r5.status_code == 422

            # Total runs > 12 (10 + 3 = 13)
            r6 = client.post("/api/benchmark/run", json={"repetitions": 10, "warmup_runs": 3})
            assert r6.status_code == 422
            assert "exceeds maximum allowed limit" in r6.text

            # timeout_per_run < 1.0
            r7 = client.post("/api/benchmark/run", json={"timeout_per_run": 0.5})
            assert r7.status_code == 422

            # timeout_per_run > 60.0
            r8 = client.post("/api/benchmark/run", json={"timeout_per_run": 61.0})
            assert r8.status_code == 422

    def test_06_uninitialized_database_returns_503(self) -> None:
        """Verify calling benchmark execution on an uninitialized database returns 503."""
        # Use TestClient without lifespan context manager
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/benchmark/run", json={"scenario": "small_workload"})
        assert resp.status_code == 503
        assert "uninitialized or closed" in resp.json()["detail"]

    def test_07_concurrent_benchmark_execution_rejected_with_409(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify concurrent benchmark executions are rejected with HTTP 409 Conflict."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Simulate acquired concurrency lock
            workload_lock.acquire()
            try:
                resp = client.post("/api/benchmark/run", json={"scenario": "small_workload"})
                assert resp.status_code == 409
                assert "Concurrent executions are not permitted" in resp.json()["detail"]
            finally:
                workload_lock.release()

            # Once released, execution succeeds
            subsequent_resp = client.post(
                "/api/benchmark/run",
                json={"scenario": "small_workload", "repetitions": 1, "warmup_runs": 0},
            )
            assert subsequent_resp.status_code == 200

    def test_08_mutual_exclusion_with_workload_execution(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify workload and benchmark executions are mutually exclusive."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Lock held by benchmark prevents workload execution
            workload_lock.acquire()
            try:
                wl_resp = client.post("/api/workload/run", json={"num_transactions": 2})
                assert wl_resp.status_code == 409
            finally:
                workload_lock.release()

            # Execute workload, then execute benchmark sequentially
            wl_ok = client.post(
                "/api/workload/run",
                json={
                    "num_transactions": 2,
                    "num_pages": 2,
                    "operations_per_transaction": 2,
                    "commit_ratio": 1.0,
                    "abort_ratio": 0.0,
                    "incomplete_ratio": 0.0,
                },
            )
            assert wl_ok.status_code == 200

            bm_ok = client.post(
                "/api/benchmark/run",
                json={"scenario": "small_workload", "repetitions": 1, "warmup_runs": 0},
            )
            assert bm_ok.status_code == 200

    def test_09_storage_safety_preserves_live_database_files(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify benchmarks in isolated temporary storage do not truncate or corrupt live database files."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # 1. Run live workload to create committed data and WAL records in the live database
            wl_resp = client.post(
                "/api/workload/run",
                json={
                    "num_transactions": 5,
                    "num_pages": 4,
                    "operations_per_transaction": 3,
                    "commit_ratio": 1.0,
                    "abort_ratio": 0.0,
                    "incomplete_ratio": 0.0,
                    "seed": 42,
                },
            )
            assert wl_resp.status_code == 200

            # Capture live file state
            db_path = tmp_path / "database.db"
            wal_path = tmp_path / "database.wal"
            assert db_path.exists()
            assert wal_path.exists()
            db_size_before = db_path.stat().st_size
            wal_size_before = wal_path.stat().st_size
            assert db_size_before > 0
            assert wal_size_before > 0

            # Capture monitoring stats
            tx_stats_before = client.get("/api/monitoring/transactions").json()
            wal_stats_before = client.get("/api/monitoring/wal").json()

            # 2. Run benchmark scenario (multiple repetitions)
            bm_resp = client.post(
                "/api/benchmark/run",
                json={
                    "scenario": "small_workload",
                    "repetitions": 3,
                    "warmup_runs": 1,
                    "seed": 99,
                },
            )
            assert bm_resp.status_code == 200

            # 3. Verify live files were NOT overwritten, deleted, or truncated
            assert db_path.exists()
            assert wal_path.exists()
            assert db_path.stat().st_size == db_size_before
            assert wal_path.stat().st_size == wal_size_before

            # 4. Verify live transaction and WAL stats were not polluted by the benchmark
            tx_stats_after = client.get("/api/monitoring/transactions").json()
            wal_stats_after = client.get("/api/monitoring/wal").json()
            assert tx_stats_after["total_started"] == tx_stats_before["total_started"]
            assert tx_stats_after["total_committed"] == tx_stats_before["total_committed"]
            assert wal_stats_after["total_records"] == wal_stats_before["total_records"]

    def test_10_benchmark_validation_failure_handling(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify validation failures return 422 and do not register false success in monitoring state."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            original_run = BenchmarkRunner.run

            def mock_failing_run(config, target_dir=None):
                real_res = original_run(config, target_dir=target_dir)
                # Create a run result marked as failed
                failed_run = BenchmarkRunResult(
                    run_index=0,
                    is_warmup=False,
                    is_success=False,
                    validation_passed=False,
                    validation_errors=["Simulated disk state mismatch after benchmark run"],
                    error_message="Simulated disk state mismatch",
                    workload_metrics=None,
                    recovery_metrics=None,
                )
                return BenchmarkScenarioResult(
                    config=config,
                    runs=[failed_run],
                    environment=real_res.environment,
                )

            monkeypatch.setattr(BenchmarkRunner, "run", mock_failing_run)

            resp = client.post(
                "/api/benchmark/run",
                json={"scenario": "small_workload", "repetitions": 1, "warmup_runs": 0},
            )
            assert resp.status_code == 422
            detail = resp.json()["detail"]
            assert "validation failed" in detail["error"]
            assert detail["failed_runs_count"] == 1

            # Monitoring state was NOT updated with a successful result
            mon_resp = client.get("/api/monitoring/benchmark")
            assert mon_resp.json()["status"] == "not_available"

    def test_11_internal_execution_failure_handling(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify internal execution errors return 500 without stack traces and preserve manager health."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            def mock_crashing_run(config, target_dir=None):
                raise BenchmarkExecutionError("Simulated internal benchmark execution crash")

            monkeypatch.setattr(BenchmarkRunner, "run", mock_crashing_run)

            resp = client.post("/api/benchmark/run", json={"scenario": "small_workload"})
            assert resp.status_code == 500
            assert "Simulated internal benchmark execution crash" in resp.json()["detail"]
            assert "Traceback" not in resp.text

            # Live database managers remain healthy
            assert not app.state.disk_manager.is_closed
            assert not app.state.log_manager.is_closed
            assert not app.state.buffer_manager.is_closed

    def test_12_recovery_scenario_benchmark_metrics(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify recovery_only scenario executes and captures mean_recovery_duration_s."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            resp = client.post(
                "/api/benchmark/run",
                json={
                    "scenario": "recovery_only",
                    "repetitions": 1,
                    "warmup_runs": 0,
                    "seed": 42,
                },
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["scenario_name"] == "recovery_only"
            assert data["mean_recovery_duration_s"] is not None
            assert data["mean_recovery_duration_s"] > 0.0

            # Verified via monitoring endpoint as well
            mon_resp = client.get("/api/monitoring/benchmark")
            mon_data = mon_resp.json()
            assert mon_data["scenario_name"] == "recovery_only"
            assert mon_data["mean_recovery_duration_s"] == data["mean_recovery_duration_s"]
