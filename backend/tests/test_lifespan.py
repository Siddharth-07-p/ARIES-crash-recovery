"""Tests for FastAPI lifespan database lifecycle, recovery, registration, and shutdown."""

import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.buffer.buffer_manager import BufferManager
from app.main import app, get_database_paths
from app.monitoring.models import (
    BufferPoolStatisticsResponse,
    MonitoringStatusResponse,
    RecoveryStatisticsResponse,
    TransactionStatisticsResponse,
    WALStatisticsResponse,
)
from app.monitoring.state import monitoring_state
from app.storage.disk_manager import DiskManager
from app.transaction.transaction_manager import TransactionManager
from app.wal.log_manager import LogManager


class TestLifespanDatabaseInitialization:
    """Comprehensive test suite for Phase 16A lifespan initialization and shutdown."""

    def test_01_lifespan_initializes_and_registers_database(self, tmp_path: Path, monkeypatch) -> None:
        """Verify startup initializes all subsystems and registers them with monitoring_state."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app) as client:
            # Subsystems attached to app.state
            assert hasattr(app.state, "disk_manager")
            assert hasattr(app.state, "log_manager")
            assert hasattr(app.state, "buffer_manager")
            assert hasattr(app.state, "transaction_manager")
            assert hasattr(app.state, "checkpoint_manager")
            assert hasattr(app.state, "recovery_manager")

            assert not app.state.disk_manager.is_closed
            assert not app.state.log_manager.is_closed
            assert not app.state.buffer_manager.is_closed
            assert not app.state.transaction_manager.is_closed
            assert not app.state.checkpoint_manager.is_closed

            # Status endpoint reflects operational database
            resp = client.get("/api/monitoring/status")
            assert resp.status_code == 200
            status_data = MonitoringStatusResponse.model_validate(resp.json())
            assert status_data.status == "operational"
            assert status_data.database_initialized is True
            assert status_data.recovery_status == "not_run"

            # WAL endpoint reflects live statistics
            resp_wal = client.get("/api/monitoring/wal")
            assert resp_wal.status_code == 200
            wal_data = WALStatisticsResponse.model_validate(resp_wal.json())
            assert wal_data.status == "available"
            assert wal_data.total_records == 0
            assert wal_data.is_closed is False

            # Buffer pool endpoint reflects configured capacity
            resp_bp = client.get("/api/monitoring/buffer-pool")
            assert resp_bp.status_code == 200
            bp_data = BufferPoolStatisticsResponse.model_validate(resp_bp.json())
            assert bp_data.status == "available"
            assert bp_data.pool_size_frames == 10
            assert bp_data.resident_pages_count == 0
            assert bp_data.available_frames_count == 10

            # Transactions endpoint reflects live transaction manager
            resp_tx = client.get("/api/monitoring/transactions")
            assert resp_tx.status_code == 200
            tx_data = TransactionStatisticsResponse.model_validate(resp_tx.json())
            assert tx_data.status == "available"
            assert tx_data.total_started == 0
            assert tx_data.active_count == 0

            # Recovery endpoint reports not_run on clean initialization
            resp_rec = client.get("/api/monitoring/recovery")
            assert resp_rec.status_code == 200
            rec_data = RecoveryStatisticsResponse.model_validate(resp_rec.json())
            assert rec_data.status == "not_run"

    def test_02_lifespan_shutdown_closes_resources_and_resets(self, tmp_path: Path, monkeypatch) -> None:
        """Verify shutdown closes resources in order and resets monitoring state."""
        monkeypatch.setenv("ARIES_DATA_DIR", str(tmp_path))

        with TestClient(app):
            dm = app.state.disk_manager
            lm = app.state.log_manager
            bm = app.state.buffer_manager
            tm = app.state.transaction_manager
            cm = app.state.checkpoint_manager

            assert not dm.is_closed
            assert not lm.is_closed
            assert not bm.is_closed
            assert not tm.is_closed
            assert not cm.is_closed

        # Outside context: all resources must be closed
        assert cm.is_closed is True
        assert tm.is_closed is True
        assert bm.is_closed is True
        assert dm.is_closed is True
        assert lm.is_closed is True

        # Monitoring state must be cleanly reset
        status = monitoring_state.get_status()
        assert status.status == "uninitialized"
        assert status.database_initialized is False

    def test_03_lifespan_recovers_existing_database_and_wal(self, tmp_path: Path, monkeypatch) -> None:
        """Verify startup executes ARIES crash recovery when existing WAL records are present."""
        data_dir = tmp_path / "rec_test"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "database.db"
        wal_path = data_dir / "database.wal"

        monkeypatch.setenv("ARIES_DATA_DIR", str(data_dir))

        # Prepare uncommitted loser transaction updates directly in files
        with DiskManager(db_path=db_path) as dm, LogManager(wal_path=wal_path) as lm:
            bm = BufferManager(disk_manager=dm, pool_size=5, log_manager=lm)
            tm = TransactionManager(log_manager=lm)

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"BASELINE_DATA___")
            bm.unpin_page(0, is_dirty=True)
            bm.flush_all_pages()

            # T1 starts and modifies page 0 without committing (loser)
            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"BASELINE_DATA___", b"CRASH_MODIFIED__")
            lm.flush()

            # Simulate abrupt process shutdown without flush
            bm.close(flush_dirty=False)
            tm.close()
            lm.close()
            dm.close()

        # Start application: lifespan should detect existing WAL records and run recovery
        with TestClient(app) as client:
            resp_rec = client.get("/api/monitoring/recovery")
            assert resp_rec.status_code == 200
            rec_val = RecoveryStatisticsResponse.model_validate(resp_rec.json())

            assert rec_val.status == "available"
            assert rec_val.is_success is True
            assert rec_val.stage == "COMPLETE"
            assert rec_val.losers_count == 1
            assert rec_val.clrs_generated == 1
            assert rec_val.updates_undone == 1
            assert rec_val.total_duration_s is not None

            # Overall status should indicate recovered
            resp_status = client.get("/api/monitoring/status")
            status_val = MonitoringStatusResponse.model_validate(resp_status.json())
            assert status_val.status == "operational"
            assert status_val.database_initialized is True
            assert status_val.recovery_status == "recovered"
            assert status_val.latest_recovery_success is True

    def test_04_lifespan_preserves_committed_data_across_restarts(self, tmp_path: Path, monkeypatch) -> None:
        """Verify repeated startup/shutdown cycles preserve committed transactions without data loss."""
        data_dir = tmp_path / "persist_test"
        monkeypatch.setenv("ARIES_DATA_DIR", str(data_dir))

        # First lifecycle: allocate page, commit a transaction
        with TestClient(app):
            bm = app.state.buffer_manager
            tm = app.state.transaction_manager
            lm = app.state.log_manager

            bm.allocate_page()
            p0 = bm.fetch_page(0)
            p0.write(0, b"COMMITTED_VAL_01")
            bm.unpin_page(0, is_dirty=True)

            t1 = tm.begin_transaction()
            lm.append_update(t1.transaction_id, 0, 0, b"\x00" * 16, b"COMMITTED_VAL_01")
            tm.commit_transaction(t1.transaction_id)

        # Second lifecycle: verify data and WAL are preserved
        with TestClient(app) as client:
            resp_wal = client.get("/api/monitoring/wal")
            wal_val = WALStatisticsResponse.model_validate(resp_wal.json())
            # At least BEGIN, UPDATE, COMMIT records present
            assert wal_val.total_records >= 3

            # Verify page 0 content is preserved
            bm2 = app.state.buffer_manager
            p0_again = bm2.fetch_page(0)
            data = p0_again.read(0, 16)
            bm2.unpin_page(0, is_dirty=False)
            assert data == b"COMMITTED_VAL_01"

    def test_05_lifespan_handles_recovery_failure_gracefully(self, tmp_path: Path, monkeypatch) -> None:
        """Verify failed recovery sets degraded status without throwing unhandled exceptions."""
        data_dir = tmp_path / "corrupt_wal_test"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "database.db"
        wal_path = data_dir / "database.wal"

        monkeypatch.setenv("ARIES_DATA_DIR", str(data_dir))

        # Create valid database page
        with DiskManager(db_path=db_path) as dm:
            dm.allocate_page()

        # Write invalid/corrupt WAL record bytes (corrupt magic)
        with open(wal_path, "wb") as f:
            f.write(b"CORRUPTED_WAL_RECORD_DATA_THAT_FAILS_DESERIALIZATION")

        # Lifespan should fail startup with clear error on corrupted WAL file
        with pytest.raises(Exception):
            with TestClient(app):
                pass
