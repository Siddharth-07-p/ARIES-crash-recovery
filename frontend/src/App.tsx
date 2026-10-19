import React, { useState } from 'react';
import { Database } from 'lucide-react';
import BenchmarkSection from './components/BenchmarkSection';
import BufferPoolSection from './components/BufferPoolSection';
import OverviewSection from './components/OverviewSection';
import RecoverySection from './components/RecoverySection';
import StatusBadge from './components/StatusBadge';
import TransactionSection from './components/TransactionSection';
import WalSection from './components/WalSection';
import WorkloadSection from './components/WorkloadSection';
import { useMonitoringData } from './hooks/useMonitoringData';
import './App.css';

export const App: React.FC = () => {
  const [pollIntervalMs, setPollIntervalMs] = useState<number>(10000);
  const { data, refresh } = useMonitoringData(pollIntervalMs);

  const statusData = data.status.data;
  const isOffline = data.health.error !== null && data.status.error !== null;
  const overallStatus = isOffline
    ? 'disconnected'
    : statusData?.status || 'uninitialized';

  return (
    <div className="app-container">
      {/* Top Header */}
      <header className="app-header" role="banner">
        <div className="header-content">
          <div className="logo-section">
            <div className="logo-icon" aria-hidden="true">
              <Database size={24} />
            </div>
            <div>
              <h1 className="logo-title">ARIES Crash Recovery System</h1>
              <span className="logo-subtitle">
                Database Internals & Observability Dashboard
              </span>
            </div>
          </div>

          <div className="header-status-group">
            <StatusBadge
              status={overallStatus}
              className="header-status-badge"
            />
          </div>
        </div>
      </header>

      {/* Main Content Area */}
      <main className="main-content" role="main">
        {/* System Overview & Health Banner */}
        <OverviewSection
          health={data.health}
          status={data.status}
          lastSuccessfulRefresh={data.lastSuccessfulRefresh}
          isRefreshing={data.isRefreshing}
          onRefresh={refresh}
          pollIntervalMs={pollIntervalMs}
          onIntervalChange={setPollIntervalMs}
        />

        {/* Dashboard Grid Sections */}
        <div className="dashboard-grid">
          {/* Transaction & WAL Column */}
          <div className="grid-column">
            <TransactionSection transactions={data.transactions} />
            <WalSection wal={data.wal} />
          </div>

          {/* Buffer Pool & Recovery Column */}
          <div className="grid-column">
            <BufferPoolSection bufferPool={data.bufferPool} />
            <RecoverySection recovery={data.recovery} />
          </div>
        </div>

        {/* Workload and Benchmark Full-Width Row */}
        <div className="dashboard-grid full-width-grid">
          <div className="grid-column">
            <WorkloadSection workload={data.workload} />
          </div>
          <div className="grid-column">
            <BenchmarkSection benchmark={data.benchmark} />
          </div>
        </div>
      </main>

      {/* Footer */}
      <footer className="app-footer" role="contentinfo">
        <div className="footer-content">
          <span>
            ARIES (Algorithms for Recovery and Isolation Exploiting Semantics) Database Engine
          </span>
          <span className="footer-dot">•</span>
          <span>Phases 0–15 Verified & Integrated</span>
        </div>
      </footer>
    </div>
  );
};

export default App;
