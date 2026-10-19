import React from 'react';
import {
  CheckCircle2,
  Clock,
  Layers,
  XCircle,
  Zap,
} from 'lucide-react';
import { EndpointState, TransactionStatisticsResponse } from '../types';
import MetricCard from './MetricCard';
import StatusBadge from './StatusBadge';

interface TransactionSectionProps {
  transactions: EndpointState<TransactionStatisticsResponse>;
}

export const TransactionSection: React.FC<TransactionSectionProps> = ({
  transactions,
}) => {
  const { data, loading, error } = transactions;
  const isUninitialized = data?.status === 'uninitialized';
  const activeIds = data?.active_transaction_ids || [];

  return (
    <section className="dashboard-section" aria-labelledby="tx-section-title">
      <div className="section-header">
        <div className="section-title-wrap">
          <Zap size={20} className="section-icon text-indigo" />
          <h3 id="tx-section-title" className="section-title">
            Transaction Manager
          </h3>
          <StatusBadge
            status={isUninitialized ? 'uninitialized' : 'available'}
            label={isUninitialized ? 'Uninitialized' : 'Active'}
          />
        </div>
        {error && <span className="section-error-pill">Error: {error}</span>}
      </div>

      <div className="metrics-grid">
        <MetricCard
          label="Total Started"
          value={data?.total_started}
          unit="txs"
          subtext="Lifetime started in current lifecycle"
          icon={<Layers size={18} />}
          accent="indigo"
          loading={loading}
          unavailable={isUninitialized}
        />

        <MetricCard
          label="Committed"
          value={data?.total_committed}
          unit="txs"
          subtext="Successfully committed & flushed"
          icon={<CheckCircle2 size={18} />}
          accent="emerald"
          loading={loading}
          unavailable={isUninitialized}
        />

        <MetricCard
          label="Aborted"
          value={data?.total_aborted}
          unit="txs"
          subtext="Explicitly aborted or rolled back"
          icon={<XCircle size={18} />}
          accent="rose"
          loading={loading}
          unavailable={isUninitialized}
        />

        <MetricCard
          label="Active (Running)"
          value={data?.active_count}
          unit="txs"
          subtext="Currently executing in RUNNING state"
          icon={<Clock size={18} />}
          accent="amber"
          loading={loading}
          unavailable={isUninitialized}
        />
      </div>

      <div className="active-transactions-panel">
        <div className="panel-title">Active Transaction Identifiers</div>
        <div className="panel-content">
          {loading ? (
            <div className="text-muted text-sm">Loading active transactions...</div>
          ) : isUninitialized ? (
            <div className="text-muted text-sm">Transaction manager not initialized</div>
          ) : activeIds.length > 0 ? (
            <div className="chip-list" aria-label="List of currently active transaction IDs">
              {activeIds.map((txId) => (
                <span key={txId} className="tx-chip">
                  <span className="tx-chip-dot" />
                  <span>Tx #{txId}</span>
                </span>
              ))}
            </div>
          ) : (
            <div className="text-muted text-sm">No active transactions in progress</div>
          )}
        </div>
      </div>
    </section>
  );
};

export default TransactionSection;
