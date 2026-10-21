"""FastAPI application entry point for ARIES Crash Recovery System."""

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
import time
from typing import AsyncGenerator, Optional, Tuple

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.buffer.buffer_manager import BufferManager
from app.buffer.constants import DEFAULT_BUFFER_POOL_SIZE
from app.checkpoint.checkpoint_manager import CheckpointManager
from app.monitoring import monitoring_router
from app.monitoring.state import monitoring_state
from app.recovery.recovery_manager import RecoveryManager
from app.storage.disk_manager import DiskManager
from app.transaction.transaction_manager import TransactionManager
from app.wal.log_manager import LogManager
from app.workload.router import router as workload_router
from app.benchmark.router import router as benchmark_router

logger = logging.getLogger("aries.main")

# Project directory resolution
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def get_database_paths() -> Tuple[Path, Path]:
    """Resolve database and WAL file paths respecting environment overrides.

    Returns:
        Tuple of (db_path, wal_path) located under project's data directory by default.
    """
    data_dir = Path(os.environ.get("ARIES_DATA_DIR", DEFAULT_DATA_DIR))
    db_path = Path(os.environ.get("ARIES_DB_PATH", data_dir / "database.db"))
    wal_path = Path(os.environ.get("ARIES_WAL_PATH", data_dir / "database.wal"))
    return db_path, wal_path


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage database engine startup, recovery, registration, and graceful shutdown."""
    db_path, wal_path = get_database_paths()
    pool_size = int(os.environ.get("ARIES_BUFFER_POOL_SIZE", DEFAULT_BUFFER_POOL_SIZE))

    dm: Optional[DiskManager] = None
    lm: Optional[LogManager] = None
    bm: Optional[BufferManager] = None
    tm: Optional[TransactionManager] = None
    cm: Optional[CheckpointManager] = None
    rm: Optional[RecoveryManager] = None

    try:
        # Ensure parent directories exist
        if db_path.parent:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        if wal_path.parent:
            wal_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Initializing ARIES database engine (db='{db_path}', wal='{wal_path}')...")

        # 1. Initialize persistent storage and logging
        dm = DiskManager(db_path=db_path)
        lm = LogManager(wal_path=wal_path)

        # 2. Initialize buffer pool and transaction manager
        bm = BufferManager(disk_manager=dm, pool_size=pool_size, log_manager=lm)
        tm = TransactionManager(log_manager=lm)
        if lm._tx_last_lsn:
            tm._next_tx_id = max(lm._tx_last_lsn.keys()) + 1

        # 3. Initialize checkpoint manager and recovery manager
        cm = CheckpointManager(log_manager=lm, transaction_manager=tm, buffer_manager=bm)
        rm = RecoveryManager(log_manager=lm, buffer_manager=bm, checkpoint_manager=cm)

        # 4. ARIES Crash Recovery on startup if existing log records are present
        if len(lm._records) > 0 or os.environ.get("ARIES_FORCE_RECOVERY", "").lower() in ("true", "1"):
            logger.info("Existing WAL log records detected; initiating ARIES crash recovery...")
            t_start = time.perf_counter()
            rec_result = rm.recover(raise_on_error=False)
            t_end = time.perf_counter()
            monitoring_state.register_recovery_result(
                rec_result,
                stage_durations={"total_duration_s": t_end - t_start},
            )
            if rec_result.is_success:
                logger.info(
                    f"ARIES recovery completed successfully ({len(rec_result.winners)} winners, "
                    f"{len(rec_result.losers)} losers, {rec_result.records_redone} redone, "
                    f"{rec_result.clrs_generated} CLRs generated)."
                )
            else:
                logger.warning(
                    f"ARIES recovery failed at {rec_result.stage}: {rec_result.error_message}"
                )

        # 5. Register active database managers with monitoring state
        monitoring_state.register_database(
            transaction_manager=tm,
            log_manager=lm,
            buffer_manager=bm,
            checkpoint_manager=cm,
            recovery_manager=rm,
        )

        # Store references in app.state for request/handler access
        app.state.disk_manager = dm
        app.state.log_manager = lm
        app.state.buffer_manager = bm
        app.state.transaction_manager = tm
        app.state.checkpoint_manager = cm
        app.state.recovery_manager = rm

        logger.info("ARIES database engine initialized and registered with monitoring state.")
        yield

    finally:
        logger.info("Shutting down ARIES database engine...")
        # Shutdown cleanup in reverse dependency order
        if cm is not None and not cm.is_closed:
            try:
                cm.close()
            except Exception as exc:
                logger.error(f"Error closing CheckpointManager: {exc}")

        if tm is not None and not tm.is_closed:
            try:
                tm.close()
            except Exception as exc:
                logger.error(f"Error closing TransactionManager: {exc}")

        if bm is not None and not bm.is_closed:
            try:
                bm.close(flush_dirty=True)
            except Exception as exc:
                logger.error(f"Error closing BufferManager: {exc}")

        if dm is not None and not dm.is_closed:
            try:
                dm.close()
            except Exception as exc:
                logger.error(f"Error closing DiskManager: {exc}")

        if lm is not None and not lm.is_closed:
            try:
                lm.close(flush=True)
            except Exception as exc:
                logger.error(f"Error closing LogManager: {exc}")

        monitoring_state.reset()
        logger.info("ARIES database engine shutdown complete.")


app = FastAPI(
    title="ARIES Crash Recovery System API",
    description="Backend API foundation for educational ARIES-inspired transactional crash recovery engine",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS configuration for local frontend development
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(monitoring_router)
app.include_router(workload_router)
app.include_router(benchmark_router)


@app.get("/")
def root():
    """Root endpoint for API discovery."""
    return {
        "service": "ARIES Crash Recovery System",
        "status": "online",
        "docs_url": "/docs",
        "health_url": "/api/health",
        "monitoring_url": "/api/monitoring/status",
        "workload_run_url": "/api/workload/run",
        "benchmark_run_url": "/api/benchmark/run",
    }


@app.get("/api/health")
def health_check():
    """Health check endpoint for status monitoring and frontend connectivity verification."""
    return {
        "status": "ok",
        "service": "ARIES Crash Recovery System",
        "version": "0.1.0",
        "message": "ARIES backend foundation is operational",
    }
