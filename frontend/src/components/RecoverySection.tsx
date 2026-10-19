import React from 'react';
import {
  AlertCircle,
  Clock,
  FileCode,
  Layers,
  RotateCcw,
  ShieldCheck,
  TrendingUp,
  Users,
} from 'lucide-react';
import { EndpointState, RecoveryStatisticsResponse } from '../types';
import MetricCard from './MetricCard';
import StatusBadge from './StatusBadge';

interface RecoverySectionProps {
  recovery: EndpointState<RecoveryStatisticsResponse>;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 0.001) {
    return `${(seconds * 1_000_000).toFixed(1)} µs`;
  }
  if (seconds < 1) {
    return `${(seconds * 1000).toFixed(2)} ms`;
  }
  return `${seconds.toFixed(3)} s`;
}

export const RecoverySection: React.FC<RecoverySectionProps> = ({ recovery }) => {
  const { data, loading, error } = recovery;
  const status = data?.status || 'not_run';
  const isNotRun = status === 'not_run';
  const isFailed = status === 'failed';

  const hasDurations =
    data?.analysis_duration_s !== null ||
    data?.redo_duration_s !== null ||
    data?.undo_duration_s !== null ||
    data?.total_duration_s !== null;

  return (
    <section className="dashboard-section" aria-labelledby="recovery-section-title">
      <div className="section-header">
        <div className="section-title-wrap">
          <ShieldCheck size={20} className="section-icon text-indigo" />
          <h3 id="recovery-section-title" className="section-title">
            ARIES Crash Recovery Engine
          </h3>
          <StatusBadge
            status={status}
            label={
              isNotRun
                ? 'Not Run'
                : isFailed
                ? 'Failed'
                : data?.is_success
                ? 'Recovery Succeeded'
                : 'Unknown'
            }
          />
        </div>
        {error && <span className="section-error-pill">Error: {error}</span>}
      </div>

      {isNotRun ? (
        <div className="empty-state-panel">
          <div className="empty-state-icon">
            <Clock size={28} className="text-muted" />
          </div>
          <div className="empty-state-text">
            <h4>No Crash Recovery Executed</h4>
            <p>
              Recovery has not run in the current application lifecycle. The ARIES recovery manager
              executes restart recovery automatically when restarting after an ungraceful shutdown.
            </p>
          </div>
        </div>
      ) : isFailed ? (
        <div className="alert-banner alert-rose" role="alert">
          <AlertCircle size={24} className="alert-icon" />
          <div>
            <h4 className="alert-title">Recovery Failed at Stage: {data?.stage || 'Unknown'}</h4>
            <p className="alert-desc">
              {data?.error_message || 'An error occurred during ARIES recovery.'}
            </p>
          </div>
        </div>
      ) : (
        <>
          <div className="metrics-grid">
            <MetricCard
              label="Recovery Stage"
              value={data?.stage || 'COMPLETE'}
              subtext="Final recovery phase completed"
              icon={<ShieldCheck size={18} />}
              accent="emerald"
              loading={loading}
            />

            <MetricCard
              label="Winner Transactions"
              value={data?.winners_count}
              unit="txs"
              subtext="Committed before crash (replayed in Redo)"
              icon={<Users size={18} />}
              accent="emerald"
              loading={loading}
            />

            <MetricCard
              label="Loser Transactions"
              value={data?.losers_count}
              unit="txs"
              subtext="Uncommitted at crash (rolled back in Undo)"
              icon={<Users size={18} />}
              accent="rose"
              loading={loading}
            />

            <MetricCard
              label="Records Redone"
              value={data?.records_redone}
              unit="records"
              subtext="History repeated during Redo pass"
              icon={<TrendingUp size={18} />}
              accent="cyan"
              loading={loading}
            />

            <MetricCard
              label="CLRs Generated"
              value={data?.clrs_generated}
              unit="CLRs"
              subtext="Compensation log records logged during Undo"
              icon={<FileCode size={18} />}
              accent="indigo"
              loading={loading}
            />

            <MetricCard
              label="Updates Undone"
              value={data?.updates_undone}
              unit="updates"
              subtext="Loser modifications rolled back"
              icon={<RotateCcw size={18} />}
              accent="amber"
              loading={loading}
            />

            <MetricCard
              label="Pages Modified"
              value={data?.pages_modified_count}
              unit="pages"
              subtext="Distinct database pages altered during recovery"
              icon={<Layers size={18} />}
              accent="indigo"
              loading={loading}
            />
          </div>

          {hasDurations && (
            <div className="timing-breakdown-panel">
              <div className="panel-title">Monotonic Stage Timing Breakdown</div>
              <div className="timing-grid">
                <div className="timing-card">
                  <span className="timing-phase">Analysis Pass</span>
                  <span className="timing-val">{formatDuration(data?.analysis_duration_s)}</span>
                  <span className="timing-sub">Reconstruct Tx Table & DPT</span>
                </div>
                <div className="timing-card">
                  <span className="timing-phase">Redo Pass</span>
                  <span className="timing-val">{formatDuration(data?.redo_duration_s)}</span>
                  <span className="timing-sub">Repeat history from min recLSN</span>
                </div>
                <div className="timing-card">
                  <span className="timing-phase">Undo Pass</span>
                  <span className="timing-val">{formatDuration(data?.undo_duration_s)}</span>
                  <span className="timing-sub">Roll back losers with CLRs</span>
                </div>
                <div className="timing-card highlight">
                  <span className="timing-phase">Total Recovery</span>
                  <span className="timing-val">{formatDuration(data?.total_duration_s)}</span>
                  <span className="timing-sub">Complete restart recovery</span>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
};

export default RecoverySection;
