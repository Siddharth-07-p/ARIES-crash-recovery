import React from 'react';
import {
  Archive,
  ArrowRight,
  CheckCircle2,
  FileText,
  HardDrive,
  ShieldAlert,
} from 'lucide-react';
import { EndpointState, WALStatisticsResponse } from '../types';
import MetricCard from './MetricCard';
import StatusBadge from './StatusBadge';

interface WalSectionProps {
  wal: EndpointState<WALStatisticsResponse>;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export const WalSection: React.FC<WalSectionProps> = ({ wal }) => {
  const { data, loading, error } = wal;
  const isUninitialized = data?.status === 'uninitialized';

  const currentLsn = data?.current_lsn;
  const flushedLsn = data?.flushed_lsn;
  const syncLag =
    currentLsn !== null && currentLsn !== undefined && flushedLsn !== null && flushedLsn !== undefined
      ? Math.max(0, currentLsn - flushedLsn)
      : null;

  return (
    <section className="dashboard-section" aria-labelledby="wal-section-title">
      <div className="section-header">
        <div className="section-title-wrap">
          <FileText size={20} className="section-icon text-cyan" />
          <h3 id="wal-section-title" className="section-title">
            Write-Ahead Logging (WAL)
          </h3>
          <StatusBadge
            status={isUninitialized ? 'uninitialized' : data?.is_closed ? 'closed' : 'open'}
            label={isUninitialized ? 'Uninitialized' : data?.is_closed ? 'Closed' : 'Active'}
          />
        </div>
        {error && <span className="section-error-pill">Error: {error}</span>}
      </div>

      <div className="metrics-grid">
        <MetricCard
          label="Current LSN"
          value={currentLsn}
          subtext="Highest allocated Log Sequence Number"
          icon={<ArrowRight size={18} />}
          accent="cyan"
          loading={loading}
          unavailable={isUninitialized}
        />

        <MetricCard
          label="Flushed LSN"
          value={flushedLsn}
          subtext="Highest durable LSN fsynced to disk"
          icon={<CheckCircle2 size={18} />}
          accent="emerald"
          loading={loading}
          unavailable={isUninitialized}
        />

        <MetricCard
          label="Durability Sync Lag"
          value={syncLag}
          unit="LSNs"
          subtext={
            syncLag === 0
              ? 'WAL is fully durable & synchronized (0 lag)'
              : 'In-flight log records pending disk flush'
          }
          icon={<ShieldAlert size={18} />}
          accent={syncLag && syncLag > 0 ? 'amber' : 'emerald'}
          loading={loading}
          unavailable={isUninitialized || syncLag === null}
        />

        <MetricCard
          label="Total Log Records"
          value={data?.total_records}
          unit="records"
          subtext="Records recorded in active WAL"
          icon={<Archive size={18} />}
          accent="indigo"
          loading={loading}
          unavailable={isUninitialized}
        />

        <MetricCard
          label="WAL File Size"
          value={formatBytes(data?.wal_file_size_bytes)}
          subtext="On-disk persistent log volume"
          icon={<HardDrive size={18} />}
          accent="cyan"
          loading={loading}
          unavailable={isUninitialized}
        />
      </div>
    </section>
  );
};

export default WalSection;
