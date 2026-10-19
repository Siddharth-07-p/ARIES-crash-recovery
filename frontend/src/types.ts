/**
 * TypeScript type definitions for ARIES Crash Recovery System monitoring dashboard.
 * Matches backend Pydantic models in `backend/app/monitoring/models.py`.
 */

export type ServiceStatus = 'operational' | 'degraded' | 'uninitialized';
export type RecoveryStatus = 'not_run' | 'recovered' | 'unrecovered' | 'failed';
export type DataAvailabilityStatus = 'available' | 'uninitialized';
export type WorkloadAvailabilityStatus = 'available' | 'not_available';
export type BenchmarkAvailabilityStatus = 'available' | 'not_available';
export type ConnectionStatus = 'checking' | 'connected' | 'disconnected';

export interface MonitoringStatusResponse {
  service: string;
  status: ServiceStatus;
  database_initialized: boolean;
  recovery_status: RecoveryStatus;
  latest_recovery_success: boolean | null;
  timestamp: string;
}

export interface TransactionStatisticsResponse {
  status: DataAvailabilityStatus;
  total_started: number | null;
  total_committed: number | null;
  total_aborted: number | null;
  active_count: number | null;
  active_transaction_ids: number[] | null;
}

export interface WALStatisticsResponse {
  status: DataAvailabilityStatus;
  current_lsn: number | null;
  flushed_lsn: number | null;
  total_records: number | null;
  wal_file_size_bytes: number | null;
  is_closed: boolean | null;
}

export interface BufferPoolStatisticsResponse {
  status: DataAvailabilityStatus;
  pool_size_frames: number | null;
  resident_pages_count: number | null;
  pinned_frames_count: number | null;
  dirty_pages_count: number | null;
  available_frames_count: number | null;
  is_closed: boolean | null;
}

export interface RecoveryStatisticsResponse {
  status: 'not_run' | 'available' | 'failed';
  is_success: boolean | null;
  stage: string | null;
  error_message: string | null;
  winners_count: number | null;
  losers_count: number | null;
  records_redone: number | null;
  clrs_generated: number | null;
  updates_undone: number | null;
  pages_modified_count: number | null;
  analysis_duration_s: number | null;
  redo_duration_s: number | null;
  undo_duration_s: number | null;
  total_duration_s: number | null;
}

export interface WorkloadSummaryResponse {
  status: WorkloadAvailabilityStatus;
  planned_operations: number | null;
  executed_operations: number | null;
  transactions_started: number | null;
  transactions_committed: number | null;
  transactions_aborted: number | null;
  transactions_incomplete: number | null;
  reads_executed: number | null;
  updates_executed: number | null;
  is_valid: boolean | null;
  crash_configured: boolean | null;
  crash_observed: boolean | null;
  fault_point: string | null;
}

export interface BenchmarkSummaryResponse {
  status: BenchmarkAvailabilityStatus;
  scenario_name: string | null;
  is_success: boolean | null;
  total_runs: number | null;
  measured_runs_count: number | null;
  warmup_runs_count: number | null;
  valid_runs_count: number | null;
  failed_runs_count: number | null;
  mean_workload_duration_s: number | null;
  mean_tx_throughput_tps: number | null;
  mean_op_throughput_ops: number | null;
  mean_recovery_duration_s: number | null;
}

export interface HealthResponse {
  status: string;
  service: string;
  version: string;
  message?: string;
}

export interface EndpointState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  lastUpdated: Date | null;
}

export interface AllMonitoringData {
  health: EndpointState<HealthResponse>;
  status: EndpointState<MonitoringStatusResponse>;
  transactions: EndpointState<TransactionStatisticsResponse>;
  wal: EndpointState<WALStatisticsResponse>;
  bufferPool: EndpointState<BufferPoolStatisticsResponse>;
  recovery: EndpointState<RecoveryStatisticsResponse>;
  workload: EndpointState<WorkloadSummaryResponse>;
  benchmark: EndpointState<BenchmarkSummaryResponse>;
  lastSuccessfulRefresh: Date | null;
  isRefreshing: boolean;
}
