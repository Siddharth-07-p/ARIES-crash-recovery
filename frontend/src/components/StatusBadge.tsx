import React from 'react';

export type BadgeVariant =
  | 'operational'
  | 'degraded'
  | 'uninitialized'
  | 'recovered'
  | 'failed'
  | 'not_run'
  | 'available'
  | 'not_available'
  | 'connected'
  | 'disconnected'
  | 'checking'
  | 'open'
  | 'closed'
  | 'valid'
  | 'invalid';

interface StatusBadgeProps {
  status: string | null | undefined;
  variant?: BadgeVariant;
  label?: string;
  className?: string;
}

export const StatusBadge: React.FC<StatusBadgeProps> = ({
  status,
  variant,
  label,
  className = '',
}) => {
  const normalized = (status || '').toLowerCase().trim();
  const effectiveVariant: BadgeVariant =
    variant ||
    (normalized === 'operational' || normalized === 'recovered' || normalized === 'connected' || normalized === 'valid'
      ? 'operational'
      : normalized === 'degraded'
      ? 'degraded'
      : normalized === 'failed' || normalized === 'disconnected' || normalized === 'invalid'
      ? 'failed'
      : normalized === 'not_run' || normalized === 'not_available'
      ? 'not_run'
      : normalized === 'open'
      ? 'available'
      : normalized === 'closed'
      ? 'degraded'
      : 'uninitialized');

  const displayLabel =
    label ||
    (normalized === 'operational'
      ? 'Operational'
      : normalized === 'degraded'
      ? 'Degraded'
      : normalized === 'uninitialized'
      ? 'Uninitialized'
      : normalized === 'recovered'
      ? 'Recovered'
      : normalized === 'failed'
      ? 'Failed'
      : normalized === 'not_run'
      ? 'Not Run'
      : normalized === 'available'
      ? 'Available'
      : normalized === 'not_available'
      ? 'Not Available'
      : normalized === 'connected'
      ? 'Connected'
      : normalized === 'disconnected'
      ? 'Offline'
      : normalized === 'checking'
      ? 'Checking...'
      : normalized === 'open'
      ? 'Open'
      : normalized === 'closed'
      ? 'Closed'
      : status || 'Unknown');

  return (
    <span className={`status-badge badge-${effectiveVariant} ${className}`}>
      <span className="badge-dot" />
      <span className="badge-text">{displayLabel}</span>
    </span>
  );
};

export default StatusBadge;
