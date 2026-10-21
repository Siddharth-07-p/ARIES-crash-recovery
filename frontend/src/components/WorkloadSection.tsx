import React from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  FileText,
  Play,
  RotateCcw,
  Zap,
} from 'lucide-react';
import { EndpointState, WorkloadSummaryResponse } from '../types';
import MetricCard from './MetricCard';
import StatusBadge from './StatusBadge';

interface WorkloadSectionProps {
  workload: EndpointState<WorkloadSummaryResponse>;
}

export const WorkloadSection: React.FC<WorkloadSectionProps> = ({ workload }) => {
  const { data, loading, error } = workload;
  const isAvailable = Boolean(
    data && (
      data.status?.toLowerCase() === 'available' ||
      (data.executed_operations !== null && data.executed_operations !== undefined && data.executed_operations > 0)
    )
  );

  return (
    <section className="dashboard-section" aria-labelledby="workload-section-title">
      <div className="section-header">
        <div className="section-title-wrap">
          <Activity size={20} className="section-icon text-cyan" />
          <h3 id="workload-section-title" className="section-title">
            Synthetic Workload Summary
          </h3>
          <StatusBadge
            status={loading && !data ? 'checking' : isAvailable ? 'available' : 'not_available'}
            label={
              loading && !data
                ? 'Loading...'
                : isAvailable
                ? 'Latest Run Available'
                : 'No Workload Run'
            }
          />
        </div>
        {error && <span className="section-error-pill">Error: {error}</span>}
      </div>

      {loading && !data ? (
        <div className="empty-state-panel">
          <div className="empty-state-icon">
            <Activity size={28} className="text-cyan spin" />
          </div>
          <div className="empty-state-text">
            <h4>Loading Synthetic Workload...</h4>
            <p>
              Retrieving latest synthetic transaction workload telemetry from database server...
            </p>
          </div>
        </div>
      ) : !isAvailable ? (
        <div className="empty-state-panel">
          <div className="empty-state-icon">
            <Play size={28} className="text-muted" />
          </div>
          <div className="empty-state-text">
            <h4>No Synthetic Workload Recorded</h4>
            <p>
              No workload has been executed in the current database server lifecycle. When workloads
              are run via the synthetic transaction workload generator, execution metrics will be
              reported here.
            </p>
          </div>
        </div>
      ) : (
        <>
          <div className="metrics-grid">
            <MetricCard
              label="Executed Operations"
              value={data?.executed_operations}
              unit={`/ ${data?.planned_operations ?? '—'} planned`}
              subtext="Total read & write operations completed"
              icon={<Activity size={18} />}
              accent="indigo"
              loading={loading}
            />

            <MetricCard
              label="Transactions Started"
              value={data?.transactions_started}
              unit="txs"
              subtext="Transactions generated in workload"
              icon={<Zap size={18} />}
              accent="cyan"
              loading={loading}
            />

            <MetricCard
              label="Committed"
              value={data?.transactions_committed}
              unit="txs"
              subtext="Committed transactions"
              icon={<CheckCircle2 size={18} />}
              accent="emerald"
              loading={loading}
            />

            <MetricCard
              label="Aborted"
              value={data?.transactions_aborted}
              unit="txs"
              subtext="Aborted & rolled back via CLRs"
              icon={<RotateCcw size={18} />}
              accent="rose"
              loading={loading}
            />

            <MetricCard
              label="Incomplete (Uncommitted)"
              value={data?.transactions_incomplete}
              unit="txs"
              subtext="Transactions interrupted or uncommitted"
              icon={<AlertTriangle size={18} />}
              accent={data?.transactions_incomplete ? 'amber' : 'slate'}
              loading={loading}
            />

            <MetricCard
              label="Read vs Update Mix"
              value={`${data?.reads_executed ?? '—'} / ${data?.updates_executed ?? '—'}`}
              subtext="READ operations vs UPDATE operations"
              icon={<FileText size={18} />}
              accent="cyan"
              loading={loading}
            />
          </div>

          <div className="workload-details-panel">
            <div className="detail-item">
              <span className="detail-label">Database Correctness Validation:</span>
              <span
                className={`detail-badge ${
                  data?.is_valid ? 'text-emerald' : 'text-rose'
                }`}
              >
                {data?.is_valid ? (
                  <>
                    <CheckCircle2 size={16} />
                    <span>State Validation Passed</span>
                  </>
                ) : (
                  <>
                    <AlertTriangle size={16} />
                    <span>Validation Discrepancy Observed</span>
                  </>
                )}
              </span>
            </div>

            <div className="detail-item">
              <span className="detail-label">Crash Fault Point:</span>
              <span className="detail-value">
                {data?.crash_configured ? (
                  <span className="text-amber">
                    Configured: <code>{data.fault_point}</code>{' '}
                    ({data.crash_observed ? 'Observed Crash' : 'Not Triggered'})
                  </span>
                ) : (
                  <span className="text-muted">None (Normal Completion)</span>
                )}
              </span>
            </div>
          </div>
        </>
      )}
    </section>
  );
};

export default WorkloadSection;
