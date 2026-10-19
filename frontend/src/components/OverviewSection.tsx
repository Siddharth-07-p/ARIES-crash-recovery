import React from 'react';
import {
  Activity,
  AlertTriangle,
  Clock,
  Database,
  RefreshCw,
  Server,
} from 'lucide-react';
import {
  EndpointState,
  HealthResponse,
  MonitoringStatusResponse,
} from '../types';
import StatusBadge from './StatusBadge';

interface OverviewSectionProps {
  health: EndpointState<HealthResponse>;
  status: EndpointState<MonitoringStatusResponse>;
  lastSuccessfulRefresh: Date | null;
  isRefreshing: boolean;
  onRefresh: () => void;
  pollIntervalMs: number;
  onIntervalChange: (intervalMs: number) => void;
}

export const OverviewSection: React.FC<OverviewSectionProps> = ({
  health,
  status,
  lastSuccessfulRefresh,
  isRefreshing,
  onRefresh,
  pollIntervalMs,
  onIntervalChange,
}) => {
  const statusData = status.data;
  const healthData = health.data;

  const isOffline = health.error !== null && status.error !== null;
  const overallStatus = isOffline
    ? 'disconnected'
    : statusData?.status || 'uninitialized';

  return (
    <section className="overview-panel" aria-label="System Overview">
      <div className="overview-header">
        <div className="overview-title-group">
          <div className="overview-badge">
            <Server size={14} />
            <span>ARIES Engine Observability</span>
          </div>
          <h2 className="overview-heading">
            {statusData?.service || healthData?.service || 'ARIES Crash Recovery System'}
          </h2>
          <p className="overview-subheading">
            Real-time inspection of database transactions, WAL records, buffer pool frames,
            crash recovery passes, and benchmark aggregates.
          </p>
        </div>

        <div className="overview-controls">
          <div className="interval-selector-group">
            <label htmlFor="poll-interval-select" className="control-label">
              <Clock size={14} />
              <span>Poll Interval:</span>
            </label>
            <select
              id="poll-interval-select"
              className="interval-select"
              value={pollIntervalMs}
              onChange={(e) => onIntervalChange(Number(e.target.value))}
              aria-label="Select automatic refresh interval"
            >
              <option value={0}>Paused (Manual)</option>
              <option value={5000}>5 seconds</option>
              <option value={10000}>10 seconds</option>
              <option value={30000}>30 seconds</option>
            </select>
          </div>

          <button
            id="manual-refresh-btn"
            className="btn-refresh"
            onClick={onRefresh}
            aria-busy={isRefreshing}
            aria-label="Refresh monitoring statistics"
            title="Fetch latest metrics from all monitoring endpoints"
          >
            <RefreshCw size={16} className={isRefreshing ? 'spin' : ''} />
            <span>{isRefreshing ? 'Refreshing...' : 'Refresh'}</span>
          </button>
        </div>
      </div>

      <div className="overview-metrics-grid">
        <div className="overview-card">
          <div className="overview-card-label">Engine Status</div>
          <div className="overview-card-content">
            <StatusBadge status={overallStatus} />
          </div>
          <div className="overview-card-caption">
            {overallStatus === 'operational'
              ? 'All subsystems healthy & responsive'
              : overallStatus === 'degraded'
              ? 'Subsystem closed or recovery failed'
              : overallStatus === 'disconnected'
              ? 'Backend unreachable on /api'
              : 'Database subsystems not yet initialized'}
          </div>
        </div>

        <div className="overview-card">
          <div className="overview-card-label">Database Subsystems</div>
          <div className="overview-card-content">
            {statusData ? (
              <span
                className={`text-indicator ${
                  statusData.database_initialized ? 'text-emerald' : 'text-muted'
                }`}
              >
                <Database size={16} />
                <span>
                  {statusData.database_initialized ? 'Initialized' : 'Uninitialized'}
                </span>
              </span>
            ) : (
              <span className="text-muted">—</span>
            )}
          </div>
          <div className="overview-card-caption">
            Buffer, WAL, and Transaction Managers
          </div>
        </div>

        <div className="overview-card">
          <div className="overview-card-label">Recovery Lifecycle</div>
          <div className="overview-card-content">
            <StatusBadge
              status={statusData?.recovery_status || 'not_run'}
              label={
                statusData?.recovery_status === 'recovered'
                  ? 'Recovered'
                  : statusData?.recovery_status === 'failed'
                  ? 'Failed'
                  : 'Not Run'
              }
            />
          </div>
          <div className="overview-card-caption">
            {statusData?.recovery_status === 'recovered'
              ? 'Restart recovery completed cleanly'
              : statusData?.recovery_status === 'failed'
              ? 'Recovery encountered an error'
              : 'No recovery run in current lifecycle'}
          </div>
        </div>

        <div className="overview-card">
          <div className="overview-card-label">Backend Health & Ping</div>
          <div className="overview-card-content">
            <span className="health-ping-text">
              <Activity size={16} className={isOffline ? 'text-rose' : 'text-emerald'} />
              <span>
                {healthData?.version ? `v${healthData.version}` : isOffline ? 'Offline' : 'Online'}
              </span>
            </span>
          </div>
          <div className="overview-card-caption">
            Last updated:{' '}
            {lastSuccessfulRefresh ? lastSuccessfulRefresh.toLocaleTimeString() : 'Never'}
          </div>
        </div>
      </div>

      {isOffline && (
        <div className="alert-banner alert-rose" role="alert">
          <AlertTriangle size={18} />
          <div>
            <strong>Backend Connection Failed:</strong> Unable to connect to FastAPI backend at{' '}
            <code>http://localhost:8000</code>. Ensure the backend server is running.
          </div>
        </div>
      )}
    </section>
  );
};

export default OverviewSection;
