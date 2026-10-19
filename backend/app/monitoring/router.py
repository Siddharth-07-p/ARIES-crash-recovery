"""FastAPI router exposing read-only database monitoring and statistics APIs."""

from fastapi import APIRouter
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

router = APIRouter(prefix="/api/monitoring", tags=["monitoring"])


@router.get("/status", response_model=MonitoringStatusResponse)
def get_service_status() -> MonitoringStatusResponse:
    """Retrieve overall service status, initialization state, and recovery condition."""
    return monitoring_state.get_status()


@router.get("/transactions", response_model=TransactionStatisticsResponse)
def get_transaction_statistics() -> TransactionStatisticsResponse:
    """Retrieve transaction metrics: started, committed, aborted, and active counts."""
    return monitoring_state.get_transaction_statistics()


@router.get("/wal", response_model=WALStatisticsResponse)
def get_wal_statistics() -> WALStatisticsResponse:
    """Retrieve Write-Ahead Logging metrics: current LSN, flushed LSN, and record count."""
    return monitoring_state.get_wal_statistics()


@router.get("/buffer-pool", response_model=BufferPoolStatisticsResponse)
def get_buffer_pool_statistics() -> BufferPoolStatisticsResponse:
    """Retrieve buffer pool frame metrics: capacity, resident pages, pins, and dirty pages."""
    return monitoring_state.get_buffer_pool_statistics()


@router.get("/recovery", response_model=RecoveryStatisticsResponse)
def get_recovery_statistics() -> RecoveryStatisticsResponse:
    """Retrieve outcome and statistics of the latest crash recovery execution."""
    return monitoring_state.get_recovery_statistics()


@router.get("/workload", response_model=WorkloadSummaryResponse)
def get_workload_summary() -> WorkloadSummaryResponse:
    """Retrieve summary metrics of the most recently executed transaction workload."""
    return monitoring_state.get_workload_summary()


@router.get("/benchmark", response_model=BenchmarkSummaryResponse)
def get_benchmark_summary() -> BenchmarkSummaryResponse:
    """Retrieve summary metrics and throughput of the most recently executed benchmark."""
    return monitoring_state.get_benchmark_summary()
