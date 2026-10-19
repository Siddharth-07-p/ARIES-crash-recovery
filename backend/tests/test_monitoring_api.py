"""Tests for read-only database monitoring and statistics APIs.

Validates endpoint responses, schemas, data types, state non-mutation, uninitialized states,
recovery reporting, workload and benchmark summaries across 16 comprehensive scenarios.
"""

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.monitoring.models import (
    BenchmarkSummaryResponse,
    BufferPoolStatisticsResponse,
    MonitoringStatusResponse,
    RecoveryStatisticsResponse,
    TransactionStatisticsResponse,
    WALStatisticsResponse,
    WorkloadSummaryResponse,
)
from app.monitoring.state import monitoring_state
from app.buffer.buffer_manager import BufferManager
from app.recovery.recovery_manager import RecoveryManager
from app.recovery.recovery_state import RecoveryResult
from app.storage.constants import PAGE_SIZE
from app.storage.disk_manager import DiskManager
from app.transaction.transaction_manager import TransactionManager
from app.wal.log_manager import LogManager
from app.workload.config import WorkloadConfig
from app.workload.executor import WorkloadExecutor
from app.benchmark.config import BenchmarkConfig
from app.benchmark.runner import BenchmarkRunner


@pytest.fixture(autouse=True)
def clean_monitoring_state():
    """Ensure monitoring_state is reset before and after every test."""
    monitoring_state.reset()
    yield
    monitoring_state.reset()


client = TestClient(app)


class TestMonitoringAPISuite:
    """Comprehensive test suite for Phase 14 monitoring and statistics APIs."""

    # -------------------------------------------------------------------------
    # 1. Monitoring status response
    # -------------------------------------------------------------------------
    def test_01_monitoring_status_response(self) -> None:
        """Verify GET /api/monitoring/status returns valid schema and status."""
        resp = client.get("/api/monitoring/status")
        assert resp.status_code == 200
        data = resp.json()

        # Validate with Pydantic model
        validated = MonitoringStatusResponse.model_validate(data)
        assert validated.service == "ARIES Crash Recovery System"
        assert validated.status == "uninitialized"
        assert validated.database_initialized is False
        assert validated.recovery_status == "not_run"
        assert validated.latest_recovery_success is None
        assert "T" in validated.timestamp

    # -------------------------------------------------------------------------
    # 2. Transaction statistics when transactions exist
    # -------------------------------------------------------------------------
    def test_02_transaction_statistics_when_transactions_exist(
        self, tmp_path: Path
    ) -> None:
        """Verify transaction counts: started, committed, aborted, and active."""
        wal_path = tmp_path / "test.wal"
        with LogManager(wal_path=wal_path) as lm, TransactionManager(log_manager=lm) as tm:
            monitoring_state.register_database(transaction_manager=tm, log_manager=lm)

            # Start 3 transactions: commit T1, abort T2, leave T3 active
            t1 = tm.begin_transaction()
            t2 = tm.begin_transaction()
            t3 = tm.begin_transaction()

            tm.commit_transaction(t1.transaction_id)
            tm.abort_transaction(t2.transaction_id)

            resp = client.get("/api/monitoring/transactions")
            assert resp.status_code == 200
            data = resp.json()

            validated = TransactionStatisticsResponse.model_validate(data)
            assert validated.status == "available"
            assert validated.total_started == 3
            assert validated.total_committed == 1
            assert validated.total_aborted == 1
            assert validated.active_count == 1
            assert validated.active_transaction_ids == [t3.transaction_id]

    # -------------------------------------------------------------------------
    # 3. WAL statistics and unavailable states
    # -------------------------------------------------------------------------
    def test_03_wal_statistics_and_unavailable_states(self, tmp_path: Path) -> None:
        """Verify WAL statistics when uninitialized vs available with records."""
        # Uninitialized state
        resp_uninit = client.get("/api/monitoring/wal")
        assert resp_uninit.status_code == 200
        val_uninit = WALStatisticsResponse.model_validate(resp_uninit.json())
        assert val_uninit.status == "uninitialized"
        assert val_uninit.current_lsn is None
        assert val_uninit.flushed_lsn is None

        # Active state with appended records
        wal_path = tmp_path / "wal_stats.wal"
        with LogManager(wal_path=wal_path) as lm:
            monitoring_state.register_database(log_manager=lm)
            lsn1 = lm.append_begin(1)
            lsn2 = lm.append_commit(1)
            lm.flush()

            resp_avail = client.get("/api/monitoring/wal")
            assert resp_avail.status_code == 200
            val_avail = WALStatisticsResponse.model_validate(resp_avail.json())
            assert val_avail.status == "available"
            assert val_avail.current_lsn == lsn2
            assert val_avail.flushed_lsn == lsn2
            assert val_avail.total_records == 2
            assert val_avail.wal_file_size_bytes is not None and val_avail.wal_file_size_bytes > 0
            assert val_avail.is_closed is False

    # -------------------------------------------------------------------------
    # 4. Buffer pool statistics and unavailable states
    # -------------------------------------------------------------------------
    def test_04_buffer_pool_statistics_and_unavailable_states(
        self, tmp_path: Path
    ) -> None:
        """Verify buffer pool statistics when uninitialized vs populated with frames."""
        # Uninitialized state
        resp_uninit = client.get("/api/monitoring/buffer-pool")
        assert resp_uninit.status_code == 200
        val_uninit = BufferPoolStatisticsResponse.model_validate(resp_uninit.json())
        assert val_uninit.status == "uninitialized"
        assert val_uninit.pool_size_frames is None

        # Active state with resident and pinned frames
        db_path = tmp_path / "bp.db"
        wal_path = tmp_path / "bp.wal"
        with DiskManager(db_path=db_path) as dm, LogManager(wal_path=wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            try:
                monitoring_state.register_database(buffer_manager=bm)

                # Allocate Page 0 (pinned) and set page LSN (marking dirty)
                p0 = bm.allocate_page()
                p0.write(0, b"TEST_DATA_BUF")
                bm.set_page_lsn(p0.page_id, 10)

                # Allocate Page 1 and unpin clean
                p1 = bm.allocate_page()
                bm.unpin_page(p1.page_id, is_dirty=False)

                resp = client.get("/api/monitoring/buffer-pool")
                assert resp.status_code == 200
                val = BufferPoolStatisticsResponse.model_validate(resp.json())

                assert val.status == "available"
                assert val.pool_size_frames == 5
                assert val.resident_pages_count == 2
                assert val.pinned_frames_count == 1  # Page 0 is still pinned
                assert val.dirty_pages_count >= 1   # Page 0 is dirty
                assert val.available_frames_count == 4  # 5 - 1 pinned
                assert val.is_closed is False

                # Unpin Page 0
                bm.unpin_page(p0.page_id, is_dirty=True)
            finally:
                bm.close(flush_dirty=False)

    # -------------------------------------------------------------------------
    # 5. Recovery statistics before and after recovery
    # -------------------------------------------------------------------------
    def test_05_recovery_statistics_before_and_after_recovery(
        self, tmp_path: Path
    ) -> None:
        """Verify recovery statistics: 'not_run' initially, then successful details after recovery."""
        # Before recovery
        resp_before = client.get("/api/monitoring/recovery")
        assert resp_before.status_code == 200
        val_before = RecoveryStatisticsResponse.model_validate(resp_before.json())
        assert val_before.status == "not_run"
        assert val_before.is_success is None

        # Run a real recovery on prepared files
        db_path = tmp_path / "rec.db"
        wal_path = tmp_path / "rec.wal"
        prep_config = WorkloadConfig(
            num_transactions=3,
            num_pages=2,
            operations_per_transaction=2,
            commit_ratio=0.33,
            abort_ratio=0.33,
            incomplete_ratio=0.34,
            auto_recover=False,
            seed=42,
        )
        WorkloadExecutor.execute(config=prep_config, db_path=db_path, wal_path=wal_path)

        rec_res = RecoveryManager.recover_from_files(db_path=db_path, wal_path=wal_path)
        monitoring_state.register_recovery_result(
            rec_res,
            stage_durations={
                "analysis_duration_s": 0.001,
                "redo_duration_s": 0.002,
                "undo_duration_s": 0.0015,
                "total_duration_s": 0.0045,
            },
        )

        resp_after = client.get("/api/monitoring/recovery")
        assert resp_after.status_code == 200
        val_after = RecoveryStatisticsResponse.model_validate(resp_after.json())

        assert val_after.status == "available"
        assert val_after.is_success is True
        assert val_after.stage == "COMPLETE"
        assert val_after.error_message is None
        assert val_after.winners_count is not None and val_after.winners_count >= 1
        assert val_after.analysis_duration_s == 0.001
        assert val_after.total_duration_s == 0.0045

    # -------------------------------------------------------------------------
    # 6. Recovery failure reporting
    # -------------------------------------------------------------------------
    def test_06_recovery_failure_reporting(self) -> None:
        """Verify that a failed recovery outcome is accurately reported."""
        failed_res = RecoveryResult(
            is_success=False,
            error_message="Simulated recovery failure during Redo",
            stage="FAILED_AT_REDO",
        )
        monitoring_state.register_recovery_result(failed_res)

        resp_rec = client.get("/api/monitoring/recovery")
        assert resp_rec.status_code == 200
        val_rec = RecoveryStatisticsResponse.model_validate(resp_rec.json())
        assert val_rec.status == "failed"
        assert val_rec.is_success is False
        assert val_rec.stage == "FAILED_AT_REDO"
        assert val_rec.error_message == "Simulated recovery failure during Redo"

        resp_status = client.get("/api/monitoring/status")
        assert resp_status.status_code == 200
        val_status = MonitoringStatusResponse.model_validate(resp_status.json())
        assert val_status.recovery_status == "failed"
        assert val_status.latest_recovery_success is False

    # -------------------------------------------------------------------------
    # 7. Workload summary when no workload has run
    # -------------------------------------------------------------------------
    def test_07_workload_summary_when_no_workload_has_run(self) -> None:
        """Verify GET /api/monitoring/workload returns not_available when no workload executed."""
        resp = client.get("/api/monitoring/workload")
        assert resp.status_code == 200
        val = WorkloadSummaryResponse.model_validate(resp.json())
        assert val.status == "not_available"
        assert val.executed_operations is None
        assert val.transactions_started is None

    # -------------------------------------------------------------------------
    # 8. Workload summary when results are available
    # -------------------------------------------------------------------------
    def test_08_workload_summary_when_results_are_available(
        self, tmp_path: Path
    ) -> None:
        """Verify GET /api/monitoring/workload returns valid summary when registered."""
        db_path = tmp_path / "wl.db"
        wal_path = tmp_path / "wl.wal"
        config = WorkloadConfig(
            num_transactions=4,
            num_pages=2,
            operations_per_transaction=2,
            commit_ratio=1.0,
            abort_ratio=0.0,
            incomplete_ratio=0.0,
            seed=101,
        )
        wl_result = WorkloadExecutor.execute(config=config, db_path=db_path, wal_path=wal_path)
        monitoring_state.register_workload_result(wl_result)

        resp = client.get("/api/monitoring/workload")
        assert resp.status_code == 200
        val = WorkloadSummaryResponse.model_validate(resp.json())

        assert val.status == "available"
        assert val.transactions_started == 4
        assert val.transactions_committed == 4
        assert val.executed_operations == wl_result.executed_operations
        assert val.is_valid is True
        assert val.crash_configured is False

    # -------------------------------------------------------------------------
    # 9. Benchmark summary when no benchmark has run
    # -------------------------------------------------------------------------
    def test_09_benchmark_summary_when_no_benchmark_has_run(self) -> None:
        """Verify GET /api/monitoring/benchmark returns not_available when no benchmark executed."""
        resp = client.get("/api/monitoring/benchmark")
        assert resp.status_code == 200
        val = BenchmarkSummaryResponse.model_validate(resp.json())
        assert val.status == "not_available"
        assert val.scenario_name is None
        assert val.total_runs is None

    # -------------------------------------------------------------------------
    # 10. Benchmark summary when results are available
    # -------------------------------------------------------------------------
    def test_10_benchmark_summary_when_results_are_available(
        self, tmp_path: Path
    ) -> None:
        """Verify GET /api/monitoring/benchmark returns summary and throughput aggregates."""
        bench_cfg = BenchmarkConfig(
            scenario_name="api_test_scenario",
            repetitions=2,
            warmup_runs=1,
            seed=404,
            workload_config=WorkloadConfig(
                num_transactions=3,
                num_pages=2,
                operations_per_transaction=2,
                commit_ratio=1.0,
                abort_ratio=0.0,
                incomplete_ratio=0.0,
                seed=404,
            ),
        )
        bench_result = BenchmarkRunner.run(bench_cfg, target_dir=tmp_path)
        monitoring_state.register_benchmark_result(bench_result)

        resp = client.get("/api/monitoring/benchmark")
        assert resp.status_code == 200
        val = BenchmarkSummaryResponse.model_validate(resp.json())

        assert val.status == "available"
        assert val.scenario_name == "api_test_scenario"
        assert val.is_success is True
        assert val.total_runs == 3
        assert val.measured_runs_count == 2
        assert val.warmup_runs_count == 1
        assert val.valid_runs_count == 2
        assert val.failed_runs_count == 0
        assert val.mean_workload_duration_s is not None and val.mean_workload_duration_s >= 0.0
        assert val.mean_tx_throughput_tps is not None and val.mean_tx_throughput_tps >= 0.0

    # -------------------------------------------------------------------------
    # 11. Response schemas and data types
    # -------------------------------------------------------------------------
    def test_11_response_schemas_and_data_types(self) -> None:
        """Verify all 7 monitoring endpoints return valid JSON matching their response models."""
        endpoints = [
            ("/api/monitoring/status", MonitoringStatusResponse),
            ("/api/monitoring/transactions", TransactionStatisticsResponse),
            ("/api/monitoring/wal", WALStatisticsResponse),
            ("/api/monitoring/buffer-pool", BufferPoolStatisticsResponse),
            ("/api/monitoring/recovery", RecoveryStatisticsResponse),
            ("/api/monitoring/workload", WorkloadSummaryResponse),
            ("/api/monitoring/benchmark", BenchmarkSummaryResponse),
        ]

        for path, model_cls in endpoints:
            resp = client.get(path)
            assert resp.status_code == 200, f"Endpoint {path} returned {resp.status_code}"
            # Assert schema validation passes without ValueError or ValidationError
            instance = model_cls.model_validate(resp.json())
            assert isinstance(instance, model_cls)

    # -------------------------------------------------------------------------
    # 12. Requests not mutating database or WAL state
    # -------------------------------------------------------------------------
    def test_12_requests_not_mutating_database_or_wal_state(
        self, tmp_path: Path
    ) -> None:
        """Verify that hitting monitoring endpoints causes zero mutations to WAL or buffer frames."""
        db_path = tmp_path / "nomut.db"
        wal_path = tmp_path / "nomut.wal"

        with DiskManager(db_path=db_path) as dm, LogManager(wal_path=wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            try:
                monitoring_state.register_database(
                    transaction_manager=tm,
                    log_manager=lm,
                    buffer_manager=bm,
                )

                t1 = tm.begin_transaction()
                p0 = bm.allocate_page()
                lsn1 = lm.append_update(t1.transaction_id, p0.page_id, 0, b"OLD", b"NEW")
                p0.write(0, b"NEW")
                bm.set_page_lsn(p0.page_id, lsn1)
                bm.unpin_page(p0.page_id, is_dirty=True)
                lm.flush()

                # Snapshot state before monitoring calls
                initial_next_lsn = lm.next_lsn
                initial_flushed_lsn = lm.flushed_lsn
                initial_wal_size = wal_path.stat().st_size
                initial_db_size = db_path.stat().st_size
                initial_tx_count = len(tm._transactions)
                initial_resident_pages = len(bm.get_resident_pages())
                initial_pin_count = bm.get_pin_count(p0.page_id)
                initial_dirty_pages = bm.dirty_page_table.get_table()
                initial_page_bytes = p0.read(0, PAGE_SIZE)

                # Send 10 requests to every monitoring endpoint
                for _ in range(10):
                    client.get("/api/monitoring/status")
                    client.get("/api/monitoring/transactions")
                    client.get("/api/monitoring/wal")
                    client.get("/api/monitoring/buffer-pool")
                    client.get("/api/monitoring/recovery")
                    client.get("/api/monitoring/workload")
                    client.get("/api/monitoring/benchmark")

                # Verify state is completely unchanged
                assert lm.next_lsn == initial_next_lsn
                assert lm.flushed_lsn == initial_flushed_lsn
                assert wal_path.stat().st_size == initial_wal_size
                assert db_path.stat().st_size == initial_db_size
                assert len(tm._transactions) == initial_tx_count
                assert len(bm.get_resident_pages()) == initial_resident_pages
                assert bm.get_pin_count(p0.page_id) == initial_pin_count
                assert bm.dirty_page_table.get_table() == initial_dirty_pages
                assert p0.read(0, PAGE_SIZE) == initial_page_bytes
            finally:
                bm.close(flush_dirty=False)

    # -------------------------------------------------------------------------
    # 13. Existing /api/health compatibility
    # -------------------------------------------------------------------------
    def test_13_existing_api_health_compatibility(self) -> None:
        """Verify GET /api/health and root GET / remain fully functional and backward-compatible."""
        # /api/health
        health_resp = client.get("/api/health")
        assert health_resp.status_code == 200
        health_data = health_resp.json()
        assert health_data["status"] == "ok"
        assert health_data["service"] == "ARIES Crash Recovery System"
        assert "version" in health_data

        # / root
        root_resp = client.get("/")
        assert root_resp.status_code == 200
        root_data = root_resp.json()
        assert root_data["status"] == "online"
        assert root_data["health_url"] == "/api/health"
        assert root_data["monitoring_url"] == "/api/monitoring/status"

    # -------------------------------------------------------------------------
    # 14. Missing or uninitialized components
    # -------------------------------------------------------------------------
    def test_14_missing_or_uninitialized_components(self) -> None:
        """Verify that all monitoring endpoints handle uninitialized components safely."""
        monitoring_state.reset()

        resp_stat = client.get("/api/monitoring/status").json()
        assert resp_stat["status"] == "uninitialized"
        assert resp_stat["database_initialized"] is False

        resp_tx = client.get("/api/monitoring/transactions").json()
        assert resp_tx["status"] == "uninitialized"
        assert resp_tx["total_started"] is None

        resp_wal = client.get("/api/monitoring/wal").json()
        assert resp_wal["status"] == "uninitialized"
        assert resp_wal["current_lsn"] is None

        resp_bp = client.get("/api/monitoring/buffer-pool").json()
        assert resp_bp["status"] == "uninitialized"
        assert resp_bp["pool_size_frames"] is None

        resp_rec = client.get("/api/monitoring/recovery").json()
        assert resp_rec["status"] == "not_run"

        resp_wl = client.get("/api/monitoring/workload").json()
        assert resp_wl["status"] == "not_available"

        resp_bm = client.get("/api/monitoring/benchmark").json()
        assert resp_bm["status"] == "not_available"

    # -------------------------------------------------------------------------
    # 15. Error handling for closed components
    # -------------------------------------------------------------------------
    def test_15_error_handling_for_closed_components(self, tmp_path: Path) -> None:
        """Verify that closed managers are reported cleanly as degraded or closed without crashing."""
        db_path = tmp_path / "closed.db"
        wal_path = tmp_path / "closed.wal"

        with DiskManager(db_path=db_path) as dm, LogManager(wal_path=wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=4, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            monitoring_state.register_database(
                transaction_manager=tm,
                log_manager=lm,
                buffer_manager=bm,
            )

            # Start a transaction and allocate page
            _ = tm.begin_transaction()
            _ = bm.allocate_page()

            # Now close the managers
            bm.close(flush_dirty=False)
            tm.close()

        # Monitoring should report degraded overall status without raising exceptions
        resp_stat = client.get("/api/monitoring/status")
        assert resp_stat.status_code == 200
        assert resp_stat.json()["status"] == "degraded"

        # Transactions endpoint should handle closed manager cleanly
        resp_tx = client.get("/api/monitoring/transactions")
        assert resp_tx.status_code == 200
        tx_data = resp_tx.json()
        assert tx_data["status"] == "available"
        assert tx_data["active_count"] == 0

        # Buffer pool endpoint should report closed status
        resp_bp = client.get("/api/monitoring/buffer-pool")
        assert resp_bp.status_code == 200
        bp_data = resp_bp.json()
        assert bp_data["status"] == "available"
        assert bp_data["is_closed"] is True

    # -------------------------------------------------------------------------
    # 16. Regression coverage for existing API routes
    # -------------------------------------------------------------------------
    def test_16_regression_coverage_for_existing_api_routes(self) -> None:
        """Verify that adding monitoring endpoints does not break route resolution or 404 handling."""
        # Non-existent routes return 404
        resp_404 = client.get("/api/non_existent_route")
        assert resp_404.status_code == 404

        # Non-existent monitoring sub-route returns 404
        resp_mon_404 = client.get("/api/monitoring/invalid_subroute")
        assert resp_mon_404.status_code == 404

        # Standard routes continue to resolve
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/api/monitoring/status").status_code == 200

    # -------------------------------------------------------------------------
    # 17. Concurrency, buffer availability, and failed recovery degradation
    # -------------------------------------------------------------------------
    def test_17_concurrency_and_closed_race_safety(self, tmp_path: Path) -> None:
        """Verify thread safety, eviction/free frame availability, and failed recovery degradation."""
        import concurrent.futures

        db_path = tmp_path / "concurrent.db"
        wal_path = tmp_path / "concurrent.wal"

        with DiskManager(db_path=db_path) as dm, LogManager(wal_path=wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=6, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            try:
                monitoring_state.register_database(
                    transaction_manager=tm,
                    log_manager=lm,
                    buffer_manager=bm,
                )

                # Allocate 2 pages: 1 pinned, 1 unpinned
                p0 = bm.allocate_page()
                p1 = bm.allocate_page()
                bm.unpin_page(p1.page_id, is_dirty=False)

                # Verify available frames calculation matches len(_free_frames) + _replacer.size()
                expected_free = len(bm._free_frames)
                expected_evictable = bm._replacer.size()
                bp_resp = client.get("/api/monitoring/buffer-pool").json()
                assert bp_resp["available_frames_count"] == expected_free + expected_evictable

                # Concurrently execute 50 monitoring requests across 5 worker threads
                errors = []

                def query_worker():
                    try:
                        r1 = client.get("/api/monitoring/status")
                        r2 = client.get("/api/monitoring/transactions")
                        r3 = client.get("/api/monitoring/wal")
                        r4 = client.get("/api/monitoring/buffer-pool")
                        if not all(r.status_code == 200 for r in [r1, r2, r3, r4]):
                            errors.append("Non-200 response")
                    except Exception as e:
                        errors.append(str(e))

                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                    futures = [executor.submit(query_worker) for _ in range(50)]
                    concurrent.futures.wait(futures)

                assert len(errors) == 0, f"Concurrent query errors: {errors}"

                # Register a failed recovery result and verify service status degrades to 'degraded'
                failed_res = RecoveryResult(
                    is_success=False,
                    stage="FAILED_AT_UNDO",
                    error_message="Test abort simulation",
                )
                monitoring_state.register_recovery_result(failed_res)

                status_resp = client.get("/api/monitoring/status").json()
                assert status_resp["status"] == "degraded"
                assert status_resp["recovery_status"] == "failed"
                assert status_resp["latest_recovery_success"] is False

                # Unpin p0 before close
                bm.unpin_page(p0.page_id, is_dirty=False)
            finally:
                bm.close(flush_dirty=False)
                tm.close()
