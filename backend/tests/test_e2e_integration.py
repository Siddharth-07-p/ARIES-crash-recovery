"""End-to-End Integration Verification Suite for ARIES Crash Recovery System (Phase 16D).

Verifies the unified interaction across:
1. Lifespan startup, database initialization, and root API discovery.
2. Initial subsystem states and unpopulated monitoring states.
3. Scenario discovery endpoint GET /api/benchmark/scenarios.
4. Live workload execution updating monitoring state, transactions, WAL, and buffer pool.
5. Benchmark execution in isolated storage updating benchmark monitoring metrics.
6. Data preservation: live database and WAL files are strictly preserved and unpolluted.
7. Mutual exclusion between workload and benchmark endpoints via concurrency lock.
8. Bounds checking and clear 422 HTTP responses for invalid inputs.
9. Safe 503 handling when database is uninitialized or closed.
10. Crash restart recovery: uncommitted transactions from workloads survive server restart
    and are recovered cleanly via ARIES Analysis, Redo, and Undo with CLRs.
"""

import hashlib
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.monitoring.models import (
    BenchmarkSummaryResponse,
    BufferPoolStatisticsResponse,
    MonitoringStatusResponse,
    TransactionStatisticsResponse,
    WALStatisticsResponse,
    WorkloadSummaryResponse,
)
from app.monitoring.state import monitoring_state
from app.workload.router import workload_lock


@pytest.fixture(autouse=True)
def reset_state():
    """Ensure clean monitoring state and free concurrency lock between tests."""
    monitoring_state.reset()
    if workload_lock.locked():
        workload_lock.release()
    yield
    monitoring_state.reset()
    if workload_lock.locked():
        workload_lock.release()


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class TestEndToEndIntegration:
    """Comprehensive Phase 16D End-to-End Integration Verification Suite."""

    def test_01_root_discovery_and_service_health(self) -> None:
        """Verify API discovery and health check endpoints."""
        with TestClient(app) as client:
            # Root endpoint exposes all service URLs
            root_resp = client.get("/")
            assert root_resp.status_code == 200
            root_data = root_resp.json()
            assert root_data["service"] == "ARIES Crash Recovery System"
            assert root_data["status"] == "online"
            assert root_data["health_url"] == "/api/health"
            assert root_data["monitoring_url"] == "/api/monitoring/status"
            assert root_data["workload_run_url"] == "/api/workload/run"
            assert root_data["benchmark_run_url"] == "/api/benchmark/run"

            # Health check endpoint
            health_resp = client.get("/api/health")
            assert health_resp.status_code == 200
            health_data = health_resp.json()
            assert health_data["status"] == "ok"
            assert health_data["version"] == "0.1.0"

    def test_02_initial_monitoring_empty_states(self, tmp_path: Path, monkeypatch) -> None:
        """Verify initial clean state before any workloads or benchmarks have executed."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Status endpoint reflects operational database
            status_resp = client.get("/api/monitoring/status")
            assert status_resp.status_code == 200
            status_data = MonitoringStatusResponse.model_validate(status_resp.json())
            assert status_data.status == "operational"
            assert status_data.database_initialized is True
            assert status_data.recovery_status == "not_run"

            # Subsystem stats start at clean zero baseline
            tx_data = TransactionStatisticsResponse.model_validate(
                client.get("/api/monitoring/transactions").json()
            )
            assert tx_data.status == "available"
            assert tx_data.total_started == 0

            wal_data = WALStatisticsResponse.model_validate(
                client.get("/api/monitoring/wal").json()
            )
            assert wal_data.status == "available"
            assert wal_data.total_records == 0

            bp_data = BufferPoolStatisticsResponse.model_validate(
                client.get("/api/monitoring/buffer-pool").json()
            )
            assert bp_data.status == "available"
            assert bp_data.pinned_frames_count == 0

            # Workload and benchmark summaries are not yet available
            wl_data = WorkloadSummaryResponse.model_validate(
                client.get("/api/monitoring/workload").json()
            )
            assert wl_data.status == "not_available"

            bm_data = BenchmarkSummaryResponse.model_validate(
                client.get("/api/monitoring/benchmark").json()
            )
            assert bm_data.status == "not_available"

    def test_03_benchmark_scenarios_discovery(self) -> None:
        """Verify preset benchmark scenarios discovery endpoint."""
        with TestClient(app) as client:
            resp = client.get("/api/benchmark/scenarios")
            assert resp.status_code == 200
            data = resp.json()
            assert "scenarios" in data
            assert len(data["scenarios"]) == 10
            assert "small_workload" in data["scenarios"]
            assert "medium_workload" in data["scenarios"]
            assert "read_heavy" in data["scenarios"]
            assert "update_heavy" in data["scenarios"]
            assert "recovery_only" in data["scenarios"]
            assert data["default_scenario"] == "small_workload"
            assert data["limits"]["max_repetitions"] == 10
            assert data["limits"]["max_warmup_runs"] == 5

    def test_04_workload_execution_and_monitoring_update_flow(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify live workload execution updates monitoring across all subsystems."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Execute synthetic workload
            payload = {
                "num_transactions": 10,
                "num_pages": 4,
                "operations_per_transaction": 5,
                "read_ratio": 0.3,
                "commit_ratio": 0.7,
                "abort_ratio": 0.2,
                "incomplete_ratio": 0.1,
                "seed": 42,
            }
            run_resp = client.post("/api/workload/run", json=payload)
            assert run_resp.status_code == 200
            run_data = run_resp.json()

            assert run_data["success"] is True
            assert run_data["is_valid"] is True
            assert run_data["transactions_started"] == 10
            assert run_data["transactions_committed"] > 0
            assert run_data["executed_operations"] > 0

            # 1. Workload summary reflects executed run
            wl_resp = client.get("/api/monitoring/workload")
            assert wl_resp.status_code == 200
            wl_data = WorkloadSummaryResponse.model_validate(wl_resp.json())
            assert wl_data.status == "available"
            assert wl_data.transactions_started == 10
            assert wl_data.transactions_committed == run_data["transactions_committed"]
            assert wl_data.transactions_aborted == run_data["transactions_aborted"]
            assert wl_data.transactions_incomplete == run_data["transactions_incomplete"]
            assert wl_data.is_valid is True

            # 2. Transaction metrics reflect active and committed transactions
            tx_resp = client.get("/api/monitoring/transactions")
            assert tx_resp.status_code == 200
            tx_data = TransactionStatisticsResponse.model_validate(tx_resp.json())
            assert tx_data.total_started == 10
            assert tx_data.total_committed == run_data["transactions_committed"]
            assert tx_data.total_aborted == run_data["transactions_aborted"]
            assert tx_data.active_count == run_data["transactions_incomplete"]

            # 3. WAL reflects logged operations and advanced LSN
            wal_resp = client.get("/api/monitoring/wal")
            assert wal_resp.status_code == 200
            wal_data = WALStatisticsResponse.model_validate(wal_resp.json())
            assert wal_data.total_records > 0
            assert wal_data.current_lsn > 0
            assert wal_data.flushed_lsn > 0

            # 4. Buffer pool reflects resident pages with zero pin leaks
            bp_resp = client.get("/api/monitoring/buffer-pool")
            assert bp_resp.status_code == 200
            bp_data = BufferPoolStatisticsResponse.model_validate(bp_resp.json())
            assert bp_data.resident_pages_count >= 4
            assert bp_data.pinned_frames_count == 0

    def test_05_benchmark_execution_and_data_preservation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify benchmark execution updates benchmark metrics without touching live DB files."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # 1. Establish persistent state in live database with workload
            client.post(
                "/api/workload/run",
                json={
                    "num_transactions": 6,
                    "num_pages": 4,
                    "operations_per_transaction": 4,
                    "commit_ratio": 1.0,
                    "abort_ratio": 0.0,
                    "incomplete_ratio": 0.0,
                    "seed": 100,
                },
            )

            db_file = tmp_path / "database.db"
            wal_file = tmp_path / "database.wal"
            assert db_file.exists()
            assert wal_file.exists()

            db_hash_before = compute_sha256(db_file)
            wal_hash_before = compute_sha256(wal_file)
            db_size_before = db_file.stat().st_size
            wal_size_before = wal_file.stat().st_size

            tx_before = client.get("/api/monitoring/transactions").json()
            wal_before = client.get("/api/monitoring/wal").json()

            # 2. Execute benchmark scenario
            bm_resp = client.post(
                "/api/benchmark/run",
                json={
                    "scenario": "small_workload",
                    "repetitions": 3,
                    "warmup_runs": 1,
                    "seed": 42,
                },
            )
            assert bm_resp.status_code == 200
            bm_data = bm_resp.json()

            assert bm_data["success"] is True
            assert bm_data["is_success"] is True
            assert bm_data["execution_mode"] == "isolated_temporary_storage"
            assert bm_data["measured_runs_count"] == 3
            assert bm_data["warmup_runs_count"] == 1
            assert bm_data["valid_runs_count"] == 3
            assert bm_data["mean_tx_throughput_tps"] > 0.0
            assert bm_data["mean_op_throughput_ops"] > 0.0

            # 3. Benchmark monitoring reflects the results
            mon_bm = client.get("/api/monitoring/benchmark").json()
            assert mon_bm["status"] == "available"
            assert mon_bm["scenario_name"] == "small_workload"
            assert mon_bm["is_success"] is True
            assert mon_bm["mean_tx_throughput_tps"] == bm_data["mean_tx_throughput_tps"]

            # 4. Strict data preservation check: live DB/WAL files must NOT change
            assert db_file.stat().st_size == db_size_before
            assert wal_file.stat().st_size == wal_size_before
            assert compute_sha256(db_file) == db_hash_before
            assert compute_sha256(wal_file) == wal_hash_before

            # 5. Live subsystem statistics must not be polluted by benchmark runs
            tx_after = client.get("/api/monitoring/transactions").json()
            wal_after = client.get("/api/monitoring/wal").json()
            assert tx_after["total_started"] == tx_before["total_started"]
            assert tx_after["total_committed"] == tx_before["total_committed"]
            assert wal_after["total_records"] == wal_before["total_records"]

    def test_06_mutual_exclusion_and_concurrency_protection(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify mutual exclusion between concurrent workload and benchmark requests."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # When concurrency lock is held:
            workload_lock.acquire()
            try:
                # Workload request is rejected with 409 Conflict
                wl_res = client.post("/api/workload/run", json={"num_transactions": 2})
                assert wl_res.status_code == 409
                assert "Concurrent executions are not permitted" in wl_res.json()["detail"]

                # Benchmark request is also rejected with 409 Conflict
                bm_res = client.post("/api/benchmark/run", json={"scenario": "small_workload"})
                assert bm_res.status_code == 409
                assert "Concurrent executions are not permitted" in bm_res.json()["detail"]
            finally:
                workload_lock.release()

            # Sequential requests execute cleanly
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

    def test_07_invalid_input_and_bounds_rejection(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify strict parameter bounds enforcement across both endpoints."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Workload invalid parameters
            assert client.post("/api/workload/run", json={"num_transactions": 0}).status_code == 422
            assert client.post("/api/workload/run", json={"num_transactions": 101}).status_code == 422
            assert client.post("/api/workload/run", json={"operations_per_transaction": 25}).status_code == 422
            assert (
                client.post(
                    "/api/workload/run",
                    json={"commit_ratio": 0.5, "abort_ratio": 0.2, "incomplete_ratio": 0.1},
                ).status_code
                == 422
            )
            assert (
                client.post(
                    "/api/workload/run",
                    json={"num_transactions": 100, "operations_per_transaction": 15},
                ).status_code
                == 422
            )

            # Benchmark invalid parameters
            assert (
                client.post("/api/benchmark/run", json={"scenario": "nonexistent"}).status_code
                == 422
            )
            assert client.post("/api/benchmark/run", json={"repetitions": 0}).status_code == 422
            assert client.post("/api/benchmark/run", json={"repetitions": 11}).status_code == 422
            assert client.post("/api/benchmark/run", json={"warmup_runs": 6}).status_code == 422
            assert (
                client.post(
                    "/api/benchmark/run",
                    json={"repetitions": 10, "warmup_runs": 3},
                ).status_code
                == 422
            )

    def test_08_uninitialized_database_safety(self) -> None:
        """Verify calling endpoints outside lifespan returns 503 Service Unavailable."""
        client = TestClient(app, raise_server_exceptions=False)
        assert client.post("/api/workload/run", json={}).status_code == 503
        assert client.post("/api/benchmark/run", json={}).status_code == 503

    def test_09_crash_recovery_after_workload_with_active_transactions(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify server restart triggers ARIES recovery for uncommitted transactions from workloads."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        # First lifespan session: execute workload with incomplete (active) transactions
        with TestClient(app) as client:
            resp = client.post(
                "/api/workload/run",
                json={
                    "num_transactions": 6,
                    "num_pages": 4,
                    "operations_per_transaction": 4,
                    "commit_ratio": 0.5,
                    "abort_ratio": 0.0,
                    "incomplete_ratio": 0.5,
                    "seed": 555,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["transactions_incomplete"] > 0

        # Second lifespan session: simulates server restart with existing WAL records
        with TestClient(app) as client_restart:
            status_resp = client_restart.get("/api/monitoring/status")
            assert status_resp.status_code == 200
            status_data = MonitoringStatusResponse.model_validate(status_resp.json())

            # Lifespan detected existing WAL records and executed ARIES crash recovery
            assert status_data.status == "operational"
            assert status_data.database_initialized is True
            assert status_data.recovery_status == "recovered"
            assert status_data.latest_recovery_success is True

            # Recovery statistics endpoint reflects winners and losers
            rec_resp = client_restart.get("/api/monitoring/recovery")
            assert rec_resp.status_code == 200
            rec_data = rec_resp.json()
            assert rec_data["status"] == "available"
            assert rec_data["is_success"] is True
            assert rec_data["winners_count"] > 0
            assert rec_data["losers_count"] > 0
            assert rec_data["clrs_generated"] >= 0
