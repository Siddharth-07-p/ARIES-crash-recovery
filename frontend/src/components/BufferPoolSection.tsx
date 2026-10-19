import React from 'react';
import {
  Flame,
  HardDrive,
  Layers,
  Lock,
  Unlock,
} from 'lucide-react';
import { BufferPoolStatisticsResponse, EndpointState } from '../types';
import MetricCard from './MetricCard';
import StatusBadge from './StatusBadge';

interface BufferPoolSectionProps {
  bufferPool: EndpointState<BufferPoolStatisticsResponse>;
}

export const BufferPoolSection: React.FC<BufferPoolSectionProps> = ({
  bufferPool,
}) => {
  const { data, loading, error } = bufferPool;
  const isUninitialized = data?.status === 'uninitialized';

  const poolSize = data?.pool_size_frames || 0;
  const residentPages = data?.resident_pages_count || 0;
  const pinnedFrames = data?.pinned_frames_count || 0;
  const dirtyPages = data?.dirty_pages_count || 0;
  const availableFrames = data?.available_frames_count || 0;

  // Disjoint physical frame partitions (sum equals poolSize)
  const cleanPages = Math.max(0, residentPages - dirtyPages);
  const freeFrames = Math.max(0, poolSize - residentPages);

  return (
    <section className="dashboard-section" aria-labelledby="buffer-section-title">
      <div className="section-header">
        <div className="section-title-wrap">
          <Layers size={20} className="section-icon text-emerald" />
          <h3 id="buffer-section-title" className="section-title">
            Buffer Pool Manager
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
          label="Total Frame Capacity"
          value={data?.pool_size_frames}
          unit="frames"
          subtext="Configured memory pool size"
          icon={<HardDrive size={18} />}
          accent="indigo"
          loading={loading}
          unavailable={isUninitialized}
        />

        <MetricCard
          label="Resident Pages"
          value={data?.resident_pages_count}
          unit="pages"
          subtext="Total pages cached in memory"
          icon={<Layers size={18} />}
          accent="cyan"
          loading={loading}
          unavailable={isUninitialized}
        />

        <MetricCard
          label="Pinned Frames"
          value={data?.pinned_frames_count}
          unit="frames"
          subtext="Active lock pin_count > 0 (non-evictable)"
          icon={<Lock size={18} />}
          accent="amber"
          loading={loading}
          unavailable={isUninitialized}
        />

        <MetricCard
          label="Dirty Pages"
          value={data?.dirty_pages_count}
          unit="pages"
          subtext="Unflushed modifications pending write"
          icon={<Flame size={18} />}
          accent="rose"
          loading={loading}
          unavailable={isUninitialized}
        />

        <MetricCard
          label="Available Frames"
          value={data?.available_frames_count}
          unit="frames"
          subtext="Free frames + unpinned frames eligible for LRU"
          icon={<Unlock size={18} />}
          accent="emerald"
          loading={loading}
          unavailable={isUninitialized}
        />
      </div>

      {!isUninitialized && poolSize > 0 && (
        <div className="buffer-visual-panel">
          <div className="panel-title">Buffer Pool Capacity Allocation (Disjoint Partition)</div>
          <div className="frame-bar-track" role="progressbar" aria-label="Buffer frame distribution" aria-valuenow={residentPages} aria-valuemin={0} aria-valuemax={poolSize}>
            {dirtyPages > 0 && (
              <div
                className="frame-bar-segment segment-dirty"
                style={{ width: `${(dirtyPages / poolSize) * 100}%` }}
                title={`Resident Dirty: ${dirtyPages} pages (${((dirtyPages / poolSize) * 100).toFixed(1)}%)`}
              />
            )}
            {cleanPages > 0 && (
              <div
                className="frame-bar-segment segment-clean"
                style={{ width: `${(cleanPages / poolSize) * 100}%` }}
                title={`Resident Clean: ${cleanPages} pages (${((cleanPages / poolSize) * 100).toFixed(1)}%)`}
              />
            )}
            {freeFrames > 0 && (
              <div
                className="frame-bar-segment segment-free"
                style={{ width: `${(freeFrames / poolSize) * 100}%` }}
                title={`Unallocated Free: ${freeFrames} frames (${((freeFrames / poolSize) * 100).toFixed(1)}%)`}
              />
            )}
          </div>

          <div className="frame-legend">
            <span className="legend-item">
              <span className="legend-dot segment-dirty" />
              <span>Resident Dirty ({dirtyPages})</span>
            </span>
            <span className="legend-item">
              <span className="legend-dot segment-clean" />
              <span>Resident Clean ({cleanPages})</span>
            </span>
            <span className="legend-item">
              <span className="legend-dot segment-free" />
              <span>Unallocated Free ({freeFrames})</span>
            </span>
          </div>

          <div className="buffer-subgrid">
            <div className="buffer-subcard">
              <div className="buffer-subcard-header">
                <span>Active Lock State</span>
                <Lock size={14} className="text-amber" />
              </div>
              <div className="buffer-subcard-val">{pinnedFrames} / {poolSize} frames</div>
              <div className="buffer-subcard-note">
                Pinned frames hold active locks (non-evictable). Pinned pages may be clean or dirty.
              </div>
            </div>

            <div className="buffer-subcard">
              <div className="buffer-subcard-header">
                <span>Replacement Eligibility</span>
                <Unlock size={14} className="text-emerald" />
              </div>
              <div className="buffer-subcard-val">{availableFrames} / {poolSize} frames</div>
              <div className="buffer-subcard-note">
                Free frames ({freeFrames}) + unpinned resident pages ({Math.max(0, residentPages - pinnedFrames)}) available for allocation.
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
};

export default BufferPoolSection;
