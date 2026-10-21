"""Tests for Phase 16B: Safe Workload-to-Monitoring Integration.

Validates:
1. Successful workload execution against the live database instance.
2. Monitoring state update and retrieval via /api/monitoring endpoints.
3. Repeated workloads executing without transaction ID collisions.
4. Input validation and upper bounds (num_transactions, operations, ratios, total ops).
5. Concurrency protection returning 409 Conflict when a workload is running.
6. Safe rejection with 503 when database is uninitialized or closed.
7. Validation failure handling preventing registration of invalid state.
8. Internal execution failure handling returning 500 without stack traces.
9. Zero resource corruption: managers remain open across successful and failed calls.
10. No regression in ARIES crash recovery semantics on workload data.
"""

import asyncio
from pathlib import Path
from typing import Any, Dict
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.monitoring.models import (
    BufferPoolStatisticsResponse,
    TransactionStatisticsResponse,
    WALStatisticsResponse,
    WorkloadSummaryResponse,
)
from app.monitoring.state import monitoring_state
from app.recovery.recovery_manager import RecoveryManager
from app.workload.exceptions import WorkloadExecutionError
from app.workload.executor import WorkloadExecutor
from app.workload.models import WorkloadResult
from app.workload.router import WorkloadRunRequest, WorkloadRunResponse, workload_lock


@pytest.fixture(autouse=True)
def reset_monitoring():
    """Reset monitoring state and release workload lock between tests."""
    monitoring_state.reset()
    if workload_lock.locked():
        workload_lock.release()
    yield
    monitoring_state.reset()
    if workload_lock.locked():
        workload_lock.release()


class TestWorkloadAPI:
    """Comprehensive test suite for Phase 16B Workload Execution API."""

    def test_01_successful_workload_execution_on_live_database(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify successful workload execution against the active lifespan database."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            payload = {
                "num_transactions": 8,
                "num_pages": 4,
                "operations_per_transaction": 4,
                "read_ratio": 0.25,
                "commit_ratio": 0.75,
                "abort_ratio": 0.25,
                "incomplete_ratio": 0.0,
                "same_page_probability": 0.2,
                "interleave_probability": 0.5,
                "seed": 42,
            }

            resp = client.post("/api/workload/run", json=payload)
            assert resp.status_code == 200, resp.text
            data = resp.json()

            validated = WorkloadRunResponse.model_validate(data)
            assert validated.success is True
            assert validated.is_valid is True
            assert validated.transactions_started == 8
            assert validated.transactions_committed + validated.transactions_aborted == 8
            assert validated.transactions_incomplete == 0
            assert validated.executed_operations > 0
            assert validated.duration_seconds > 0.0
            assert len(validated.validation_errors) == 0

            # Subsystem managers in app.state remain open and functional
            assert not app.state.disk_manager.is_closed
            assert not app.state.log_manager.is_closed
            assert not app.state.buffer_manager.is_closed
            assert not app.state.transaction_manager.is_closed
            assert app.state.disk_manager.get_page_count() >= 4

    def test_02_workload_result_registered_and_retrievable_via_monitoring(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify workload outcome is registered with monitoring_state and visible on endpoints."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Initially workload summary is not available
            pre_resp = client.get("/api/monitoring/workload")
            assert pre_resp.status_code == 200
            pre_data = WorkloadSummaryResponse.model_validate(pre_resp.json())
            assert pre_data.status == "not_available"

            # Execute workload
            payload = {
                "num_transactions": 6,
                "num_pages": 3,
                "operations_per_transaction": 3,
                "read_ratio": 0.3,
                "commit_ratio": 0.6,
                "abort_ratio": 0.2,
                "incomplete_ratio": 0.2,
                "seed": 101,
            }
            exec_resp = client.post("/api/workload/run", json=payload)
            assert exec_resp.status_code == 200
            run_data = exec_resp.json()

            # Monitoring endpoint now reports 'available' with matching metrics
            post_resp = client.get("/api/monitoring/workload")
            assert post_resp.status_code == 200
            post_data = WorkloadSummaryResponse.model_validate(post_resp.json())
            assert post_data.status == "available"
            assert post_data.transactions_started == run_data["transactions_started"]
            assert post_data.transactions_committed == run_data["transactions_committed"]
            assert post_data.transactions_aborted == run_data["transactions_aborted"]
            assert post_data.transactions_incomplete == run_data["transactions_incomplete"]
            assert post_data.executed_operations == run_data["executed_operations"]
            assert post_data.is_valid is True

            # Transaction monitoring reflects executed transactions
            tx_resp = client.get("/api/monitoring/transactions")
            assert tx_resp.status_code == 200
            tx_data = TransactionStatisticsResponse.model_validate(tx_resp.json())
            assert tx_data.status == "available"
            assert tx_data.total_started >= 6

            # WAL monitoring reflects appended log records
            wal_resp = client.get("/api/monitoring/wal")
            assert wal_resp.status_code == 200
            wal_data = WALStatisticsResponse.model_validate(wal_resp.json())
            assert wal_data.status == "available"
            assert wal_data.total_records > 0
            assert wal_data.current_lsn > 0

            # Buffer pool monitoring reflects resident pages with zero pin leaks
            bp_resp = client.get("/api/monitoring/buffer-pool")
            assert bp_resp.status_code == 200
            bp_data = BufferPoolStatisticsResponse.model_validate(bp_resp.json())
            assert bp_data.status == "available"
            assert bp_data.pinned_frames_count == 0

    def test_03_repeated_workloads_accumulate_safely(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify multiple successive workloads execute without transaction ID collisions."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            payload_1 = {
                "num_transactions": 5,
                "num_pages": 4,
                "operations_per_transaction": 3,
                "commit_ratio": 1.0,
                "abort_ratio": 0.0,
                "incomplete_ratio": 0.0,
                "seed": 1,
            }
            resp_1 = client.post("/api/workload/run", json=payload_1)
            assert resp_1.status_code == 200
            data_1 = resp_1.json()
            assert data_1["transactions_committed"] == 5

            payload_2 = {
                "num_transactions": 5,
                "num_pages": 4,
                "operations_per_transaction": 3,
                "commit_ratio": 1.0,
                "abort_ratio": 0.0,
                "incomplete_ratio": 0.0,
                "seed": 2,
            }
            resp_2 = client.post("/api/workload/run", json=payload_2)
            assert resp_2.status_code == 200
            data_2 = resp_2.json()
            assert data_2["transactions_committed"] == 5

            # Transaction IDs must not overlap between the two runs
            tx_ids_1 = set(data_1["committed_transactions"])
            tx_ids_2 = set(data_2["committed_transactions"])
            assert len(tx_ids_1.intersection(tx_ids_2)) == 0
            assert max(tx_ids_2) > max(tx_ids_1)

            # Cumulative transactions in TM should be at least 10
            tx_resp = client.get("/api/monitoring/transactions")
            assert tx_resp.status_code == 200
            tx_stats = tx_resp.json()
            assert tx_stats["total_committed"] >= 10

    def test_04_execute_alias_endpoint(self, tmp_path: Path, monkeypatch) -> None:
        """Verify POST /api/workload/execute behaves identically to /run."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            resp = client.post(
                "/api/workload/execute",
                json={
                    "num_transactions": 4,
                    "num_pages": 2,
                    "operations_per_transaction": 2,
                    "commit_ratio": 1.0,
                    "abort_ratio": 0.0,
                    "incomplete_ratio": 0.0,
                    "seed": 99,
                },
            )
            assert resp.status_code == 200
            assert resp.json()["success"] is True

    def test_05_invalid_parameter_bounds_rejected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify bounds checking and 422 HTTP responses for invalid request parameters."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # num_transactions < 1
            r1 = client.post("/api/workload/run", json={"num_transactions": 0})
            assert r1.status_code == 422

            # num_transactions > 100
            r2 = client.post("/api/workload/run", json={"num_transactions": 101})
            assert r2.status_code == 422

            # num_pages < 1
            r3 = client.post("/api/workload/run", json={"num_pages": 0})
            assert r3.status_code == 422

            # num_pages > 32
            r4 = client.post("/api/workload/run", json={"num_pages": 33})
            assert r4.status_code == 422

            # operations_per_transaction > 20
            r5 = client.post("/api/workload/run", json={"operations_per_transaction": 21})
            assert r5.status_code == 422

            # Total operations > 1000 (100 * 15 = 1500)
            r6 = client.post(
                "/api/workload/run",
                json={"num_transactions": 100, "operations_per_transaction": 15},
            )
            assert r6.status_code == 422
            assert "exceeds maximum allowed limit" in r6.text

            # Ratios do not sum to 1.0
            r7 = client.post(
                "/api/workload/run",
                json={
                    "commit_ratio": 0.5,
                    "abort_ratio": 0.2,
                    "incomplete_ratio": 0.1,  # sum = 0.8
                },
            )
            assert r7.status_code == 422
            assert "must sum to 1.0" in r7.text

    def test_06_uninitialized_database_returns_503(self) -> None:
        """Verify calling workload execution on an uninitialized database returns 503."""
        # Use TestClient without lifespan context manager
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/workload/run", json={})
        assert resp.status_code == 503
        assert "uninitialized or closed" in resp.json()["detail"]

    def test_07_concurrent_execution_returns_409_conflict(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify concurrent workload executions are rejected with HTTP 409 Conflict."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Simulate an already-acquired workload lock
            workload_lock.acquire()
            try:
                resp = client.post("/api/workload/run", json={})
                assert resp.status_code == 409
                assert "Concurrent executions are not permitted" in resp.json()["detail"]
            finally:
                workload_lock.release()

            # Verify once lock is released, execution succeeds
            subsequent_resp = client.post(
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
            assert subsequent_resp.status_code == 200

    def test_08_validation_failure_handling(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify that validation failures return 422 and do not register a false success."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Monkeypatch execute_live to simulate a state validation failure
            original_execute_live = WorkloadExecutor.execute_live

            def mock_failing_execute_live(*args, **kwargs):
                res = original_execute_live(*args, **kwargs)
                # Tamper result to simulate validation failure
                return WorkloadResult(
                    config=res.config,
                    seed=res.seed,
                    planned_operations=res.planned_operations,
                    executed_operations=res.executed_operations,
                    transactions_started=res.transactions_started,
                    transactions_committed=res.transactions_committed,
                    transactions_aborted=res.transactions_aborted,
                    transactions_incomplete=res.transactions_incomplete,
                    reads_executed=res.reads_executed,
                    updates_executed=res.updates_executed,
                    crash_configured=False,
                    crash_observed=False,
                    is_valid=False,
                    validation_errors=["Simulated disk checksum mismatch error on page 2"],
                )

            monkeypatch.setattr(WorkloadExecutor, "execute_live", mock_failing_execute_live)

            resp = client.post(
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
            assert resp.status_code == 422
            detail = resp.json()["detail"]
            assert "validation failed" in detail["error"]
            assert len(detail["validation_errors"]) > 0

            # Verify monitoring state was NOT registered with success
            mon_resp = client.get("/api/monitoring/workload")
            assert mon_resp.json()["status"] == "not_available"

    def test_09_internal_execution_failure_handling(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify internal execution errors return 500 without stack traces and keep DB open."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            def mock_crashing_execute_live(*args, **kwargs):
                raise WorkloadExecutionError("Simulated write failure during log flush")

            monkeypatch.setattr(WorkloadExecutor, "execute_live", mock_crashing_execute_live)

            resp = client.post("/api/workload/run", json={"num_transactions": 3})
            assert resp.status_code == 500
            assert "Simulated write failure during log flush" in resp.json()["detail"]
            assert "Traceback" not in resp.text

            # Subsystem managers must not be closed
            assert not app.state.disk_manager.is_closed
            assert not app.state.log_manager.is_closed
            assert not app.state.buffer_manager.is_closed

    def test_10_recovery_guarantee_after_workload_with_incomplete_transactions(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Verify ARIES recovery preserves crash recovery guarantees after workload execution."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Run workload leaving some incomplete (loser) transactions
            resp = client.post(
                "/api/workload/run",
                json={
                    "num_transactions": 6,
                    "num_pages": 4,
                    "operations_per_transaction": 4,
                    "commit_ratio": 0.5,
                    "abort_ratio": 0.0,
                    "incomplete_ratio": 0.5,
                    "seed": 777,
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["transactions_incomplete"] > 0
            incomplete_tx_ids = set(data["incomplete_transactions"])
            committed_tx_ids = set(data["committed_transactions"])

        # Outside lifespan, the database is flushed and closed cleanly.
        # Now instantiate a standalone RecoveryManager to simulate post-crash recovery
        data_dir = tmp_path
        db_path = data_dir / "database.db"
        wal_path = data_dir / "database.wal"
        assert db_path.exists()
        assert wal_path.exists()

        from app.storage.disk_manager import DiskManager
        from app.wal.log_manager import LogManager
        from app.buffer.buffer_manager import BufferManager

        with DiskManager(db_path) as dm, LogManager(wal_path) as lm:
            bm = BufferManager(dm, pool_size=10, log_manager=lm)
            try:
                rm = RecoveryManager(log_manager=lm, buffer_manager=bm)
                rec_result = rm.recover()

                assert rec_result.is_success is True
                assert set(rec_result.winners) == committed_tx_ids
                assert set(rec_result.losers) == incomplete_tx_ids
                assert rec_result.clrs_generated >= 0
            finally:
                bm.close(flush_dirty=True)
