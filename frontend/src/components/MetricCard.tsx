import React from 'react';

interface MetricCardProps {
  label: string;
  value: React.ReactNode;
  unit?: string;
  subtext?: string;
  icon?: React.ReactNode;
  accent?: 'cyan' | 'indigo' | 'emerald' | 'rose' | 'amber' | 'slate';
  loading?: boolean;
  unavailable?: boolean;
  tooltip?: string;
  className?: string;
  badge?: React.ReactNode;
}

export const MetricCard: React.FC<MetricCardProps> = ({
  label,
  value,
  unit,
  subtext,
  icon,
  accent = 'indigo',
  loading = false,
  unavailable = false,
  tooltip,
  className = '',
  badge,
}) => {
  const isValueEmpty = value === null || value === undefined;

  return (
    <article
      className={`metric-card accent-${accent} ${className} ${loading ? 'loading' : ''}`}
      title={tooltip}
    >
      <div className="metric-card-header">
        <span className="metric-card-label">{label}</span>
        {icon && <span className="metric-card-icon">{icon}</span>}
      </div>

      <div className="metric-card-body">
        {loading ? (
          <div className="metric-shimmer" aria-label="Loading metric value" />
        ) : unavailable || isValueEmpty ? (
          <div className="metric-card-value text-muted" aria-label={`${label}: Unavailable`}>
            <span>—</span>
            {unit && <span className="metric-card-unit">{unit}</span>}
          </div>
        ) : (
          <div className="metric-card-value">
            <span>{value}</span>
            {unit && <span className="metric-card-unit">{unit}</span>}
          </div>
        )}
      </div>

      {(subtext || badge) && (
        <div className="metric-card-footer">
          {subtext && <span className="metric-card-subtext">{subtext}</span>}
          {badge && <span className="metric-card-badge-wrapper">{badge}</span>}
        </div>
      )}
    </article>
  );
};

export default MetricCard;
