import React from 'react';
import {
  CheckCircle2,
  Clock,
  Cpu,
  Layers,
  TrendingUp,
  XCircle,
  Zap,
} from 'lucide-react';
import { BenchmarkSummaryResponse, EndpointState } from '../types';
import MetricCard from './MetricCard';
import StatusBadge from './StatusBadge';

interface BenchmarkSectionProps {
  benchmark: EndpointState<BenchmarkSummaryResponse>;
}

export const BenchmarkSection: React.FC<BenchmarkSectionProps> = ({ benchmark }) => {
  const { data, loading, error } = benchmark;
  const isAvailable = data?.status === 'available';

  return (
    <section className="dashboard-section" aria-labelledby="benchmark-section-title">
      <div className="section-header">
        <div className="section-title-wrap">
          <TrendingUp size={20} className="section-icon text-indigo" />
          <h3 id="benchmark-section-title" className="section-title">
            Performance Benchmarks
          </h3>
          <StatusBadge
            status={isAvailable ? 'available' : 'not_available'}
            label={isAvailable ? 'Latest Benchmark' : 'No Benchmark Run'}
          />
        </div>
        {error && <span className="section-error-pill">Error: {error}</span>}
      </div>

      {!isAvailable ? (
        <div className="empty-state-panel">
          <div className="empty-state-icon">
            <Cpu size={28} className="text-muted" />
          </div>
          <div className="empty-state-text">
            <h4>No Benchmark Results Available</h4>
            <p>
              No performance benchmark scenario has been executed in the current process lifecycle.
              Run benchmarks via <code>python -m app.benchmark</code> to populate throughput and
              recovery timing distributions.
            </p>
          </div>
        </div>
      ) : (
        <>
          <div className="benchmark-scenario-banner">
            <div>
              <span className="scenario-label">Scenario Name</span>
              <h4 className="scenario-title">{data?.scenario_name || 'Custom Benchmark'}</h4>
            </div>
            <div className="scenario-status">
              {data?.is_success ? (
                <span className="badge-pill pill-emerald">
                  <CheckCircle2 size={16} />
                  <span>Validation Passed</span>
                </span>
              ) : (
                <span className="badge-pill pill-rose">
                  <XCircle size={16} />
                  <span>Validation Failed</span>
                </span>
              )}
            </div>
          </div>

          <div className="metrics-grid">
            <MetricCard
              label="Transaction Throughput"
              value={
                data?.mean_tx_throughput_tps !== null && data?.mean_tx_throughput_tps !== undefined
                  ? data.mean_tx_throughput_tps.toFixed(2)
                  : null
              }
              unit="TPS"
              subtext="Mean committed/completed transactions per second"
              icon={<Zap size={18} />}
              accent="emerald"
              loading={loading}
            />

            <MetricCard
              label="Operation Throughput"
              value={
                data?.mean_op_throughput_ops !== null && data?.mean_op_throughput_ops !== undefined
                  ? data.mean_op_throughput_ops.toFixed(2)
                  : null
              }
              unit="ops/sec"
              subtext="Mean operations executed per second"
              icon={<TrendingUp size={18} />}
              accent="cyan"
              loading={loading}
            />

            <MetricCard
              label="Mean Workload Duration"
              value={
                data?.mean_workload_duration_s !== null && data?.mean_workload_duration_s !== undefined
                  ? data.mean_workload_duration_s.toFixed(4)
                  : null
              }
              unit="seconds"
              subtext="Mean duration across measured repetitions"
              icon={<Clock size={18} />}
              accent="indigo"
              loading={loading}
            />

            <MetricCard
              label="Mean Recovery Duration"
              value={
                data?.mean_recovery_duration_s !== null && data?.mean_recovery_duration_s !== undefined
                  ? data.mean_recovery_duration_s.toFixed(4)
                  : null
              }
              unit="seconds"
              subtext={
                data?.mean_recovery_duration_s !== null
                  ? 'Mean crash recovery restart duration'
                  : 'Recovery timing not included in this scenario'
              }
              icon={<Clock size={18} />}
              accent="amber"
              loading={loading}
              unavailable={data?.mean_recovery_duration_s === null}
            />

            <MetricCard
              label="Total Repetitions"
              value={data?.total_runs}
              unit="runs"
              subtext={`${data?.measured_runs_count ?? '—'} measured + ${data?.warmup_runs_count ?? '—'} warmup`}
              icon={<Layers size={18} />}
              accent="indigo"
              loading={loading}
            />

            <MetricCard
              label="Valid / Failed Runs"
              value={`${data?.valid_runs_count ?? '—'} / ${data?.failed_runs_count ?? '—'}`}
              subtext="Runs passing correctness check"
              icon={<CheckCircle2 size={18} />}
              accent={data?.failed_runs_count && data.failed_runs_count > 0 ? 'rose' : 'emerald'}
              loading={loading}
            />
          </div>
        </>
      )}
    </section>
  );
};

export default BenchmarkSection;
