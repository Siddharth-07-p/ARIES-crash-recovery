import React, { useState, useEffect, useCallback } from 'react';
import { 
  Database, 
  Activity, 
  RefreshCw, 
  Layers, 
  HardDrive, 
  FileText, 
  ShieldCheck, 
  Zap, 
  CheckCircle2, 
  AlertCircle 
} from 'lucide-react';
import { HealthResponse, ConnectionStatus } from './types';
import './App.css';

export const App: React.FC = () => {
  const [status, setStatus] = useState<ConnectionStatus>('checking');
  const [healthData, setHealthData] = useState<HealthResponse | null>(null);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const checkHealth = useCallback(async () => {
    setIsRefreshing(true);
    try {
      // Fetch from relative /api/health (routed via Vite proxy or direct)
      const res = await fetch('/api/health');
      if (!res.ok) {
        throw new Error(`HTTP error! status: ${res.status}`);
      }
      const data: HealthResponse = await res.json();
      setHealthData(data);
      setStatus('connected');
      setErrorMsg(null);
    } catch (err: unknown) {
      console.error('Failed to connect to backend:', err);
      setStatus('disconnected');
      setHealthData(null);
      setErrorMsg(err instanceof Error ? err.message : 'Connection failed');
    } finally {
      setLastChecked(new Date());
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    checkHealth();
    // Poll every 10 seconds for seamless development monitoring
    const interval = setInterval(checkHealth, 10000);
    return () => clearInterval(interval);
  }, [checkHealth]);

  const roadmapItems = [
    {
      phase: 'Phase 0',
      title: 'Project Setup & Architecture',
      desc: 'Python FastAPI backend foundation, React TypeScript frontend, testing and health verification.',
      status: 'Current Phase',
      icon: <CheckCircle2 size={18} className="text-emerald" />,
      active: true,
    },
    {
      phase: 'Phase 1',
      title: 'Page-Based Persistent Storage',
      desc: 'Slotted page architecture, disk manager, binary serialization, and file I/O.',
      status: 'Upcoming',
      icon: <HardDrive size={18} />,
      active: false,
    },
    {
      phase: 'Phase 2',
      title: 'Buffer Pool Manager',
      desc: 'Frame pool, replacement policies (Clock/LRU), Dirty Page Table (DPT) & recLSN tracking.',
      status: 'Upcoming',
      icon: <Layers size={18} />,
      active: false,
    },
    {
      phase: 'Phase 3',
      title: 'Write-Ahead Logging (WAL)',
      desc: 'Append-only log records, LSN sequence numbering, pageLSN, and fuzzy checkpoints.',
      status: 'Upcoming',
      icon: <FileText size={18} />,
      active: false,
    },
    {
      phase: 'Phase 4',
      title: 'Transaction Manager',
      desc: 'ACID transaction states, Transaction Table, commit/abort protocols.',
      status: 'Upcoming',
      icon: <Zap size={18} />,
      active: false,
    },
    {
      phase: 'Phase 5',
      title: 'ARIES Recovery Engine',
      desc: 'Analysis pass, Redo pass (repeating history), Undo pass, and Compensation Log Records (CLRs).',
      status: 'Upcoming',
      icon: <ShieldCheck size={18} />,
      active: false,
    },
  ];

  return (
    <div className="app-container">
      {/* Header */}
      <header className="app-header">
        <div className="header-content">
          <div className="logo-section">
            <div className="logo-icon">
              <Database size={24} />
            </div>
            <div>
              <h1 className="logo-title">ARIES Crash Recovery System</h1>
              <span className="logo-subtitle">Database Internals & Recovery Engine</span>
            </div>
          </div>

          <div
            id="backend-status-indicator"
            className={`status-pill ${status}`}
          >
            <span className="status-dot"></span>
            <span>
              {status === 'connected' && 'Backend: Connected'}
              {status === 'disconnected' && 'Backend: Not Connected'}
              {status === 'checking' && 'Backend: Connecting...'}
            </span>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="main-content">
        {/* Hero & Connection Dashboard Card */}
        <section className="hero-card">
          <div className="hero-header">
            <div>
              <div className="hero-badge">Phase 0 Foundation</div>
              <h2 className="hero-title">System Status & Architectural Foundation</h2>
              <p className="hero-desc">
                Welcome to the ARIES Crash Recovery development dashboard. Phase 0 establishes the
                communication pipeline between the React/TypeScript frontend and the FastAPI backend.
              </p>
            </div>
            <button
              id="refresh-health-btn"
              onClick={checkHealth}
              disabled={isRefreshing}
              className="refresh-btn"
              title="Refresh backend status"
            >
              <RefreshCw size={16} className={isRefreshing ? 'spin' : ''} />
              <span>{isRefreshing ? 'Checking...' : 'Check Status'}</span>
            </button>
          </div>

          <div className="health-panel">
            <div className="metric-box">
              <div className="metric-label">Backend Status</div>
              <div className="metric-value">
                {status === 'connected' ? (
                  <span style={{ color: 'var(--accent-emerald)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <CheckCircle2 size={16} /> Online
                  </span>
                ) : status === 'disconnected' ? (
                  <span style={{ color: 'var(--accent-rose)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <AlertCircle size={16} /> Offline
                  </span>
                ) : (
                  <span style={{ color: 'var(--accent-amber)' }}>Probing...</span>
                )}
              </div>
            </div>

            <div className="metric-box">
              <div className="metric-label">Service Name</div>
              <div className="metric-value">
                {healthData?.service || (status === 'disconnected' ? 'Unavailable' : 'Loading...')}
              </div>
            </div>

            <div className="metric-box">
              <div className="metric-label">Backend Version</div>
              <div className="metric-value">
                {healthData?.version ? `v${healthData.version}` : 'N/A'}
              </div>
            </div>

            <div className="metric-box">
              <div className="metric-label">Last Health Ping</div>
              <div className="metric-value">
                {lastChecked ? lastChecked.toLocaleTimeString() : 'Never'}
              </div>
            </div>
          </div>

          {errorMsg && (
            <div style={{ marginTop: '1rem', color: 'var(--accent-rose)', fontSize: '0.85rem' }}>
              Error: {errorMsg} (Ensure FastAPI backend is running on port 8000)
            </div>
          )}
        </section>

        {/* Roadmap / Architecture Preview Section */}
        <section>
          <h3 className="section-title">
            <Activity size={20} style={{ color: 'var(--accent-indigo)' }} />
            <span>Implementation Roadmap</span>
          </h3>

          <div className="roadmap-grid">
            {roadmapItems.map((item, index) => (
              <div
                key={index}
                className={`phase-card ${item.active ? 'active' : ''}`}
              >
                <div className="phase-top">
                  <span className="phase-tag">{item.phase}</span>
                  <span className={`phase-badge ${item.active ? 'current' : ''}`}>
                    {item.status}
                  </span>
                </div>
                <h4 className="phase-title">{item.title}</h4>
                <p className="phase-desc">{item.desc}</p>
              </div>
            ))}
          </div>
        </section>
      </main>

      {/* Footer */}
      <footer className="app-footer">
        ARIES-Inspired Database Crash Recovery System &mdash; Phase 0 Foundation
      </footer>
    </div>
  );
};

export default App;
