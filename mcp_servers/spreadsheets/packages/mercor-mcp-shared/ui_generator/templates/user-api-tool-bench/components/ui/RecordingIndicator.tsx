/**
 * Recording indicator component for trajectory capture.
 *
 * Displays a pulsing red badge when trajectory recording is active.
 * Use alongside useTrajectoryRecording hook to show recording status.
 *
 * Usage:
 *   const { isRecording } = useTrajectoryRecording();
 *   return <RecordingIndicator isRecording={isRecording} />;
 */

import React from 'react';

export interface RecordingIndicatorProps {
  /** Whether recording is currently active */
  isRecording: boolean;
  /** Optional custom label (default: "Recording") */
  label?: string;
  /** Optional additional CSS classes */
  className?: string;
  /** Variant: 'badge' for inline badge, 'minimal' for just the dot */
  variant?: 'badge' | 'minimal';
}

/**
 * A pulsing red indicator showing that trajectory recording is active.
 * Renders nothing when isRecording is false.
 */
export function RecordingIndicator({
  isRecording,
  label = 'Recording',
  className = '',
  variant = 'badge',
}: RecordingIndicatorProps): React.ReactElement | null {
  if (!isRecording) {
    return null;
  }

  const dotStyle: React.CSSProperties = {
    width: '8px',
    height: '8px',
    backgroundColor: variant === 'badge' ? '#fff' : '#ef4444',
    borderRadius: '50%',
    display: 'inline-block',
  };

  const pulseKeyframes = `
    @keyframes recording-pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }
  `;

  if (variant === 'minimal') {
    return (
      <>
        <style>{pulseKeyframes}</style>
        <span
          className={className}
          style={{
            ...dotStyle,
            animation: 'recording-pulse 2s infinite',
          }}
          title="Recording trajectory"
          aria-label="Recording active"
        />
      </>
    );
  }

  // Badge variant - Bootstrap compatible classes
  return (
    <>
      <style>{pulseKeyframes}</style>
      <span
        className={`badge bg-danger d-flex align-items-center gap-1 ${className}`.trim()}
        style={{ animation: 'recording-pulse 2s infinite' }}
        title="Trajectory recording is active"
        aria-label="Recording active"
      >
        <span style={dotStyle} />
        {label}
      </span>
    </>
  );
}

/**
 * Tailwind CSS variant of the recording indicator.
 * Use this if your UI uses Tailwind instead of Bootstrap.
 */
export function RecordingIndicatorTailwind({
  isRecording,
  label = 'Recording',
  className = '',
  variant = 'badge',
}: RecordingIndicatorProps): React.ReactElement | null {
  if (!isRecording) {
    return null;
  }

  if (variant === 'minimal') {
    return (
      <span
        className={`w-2 h-2 bg-red-500 rounded-full animate-pulse ${className}`.trim()}
        title="Recording trajectory"
        aria-label="Recording active"
      />
    );
  }

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-full bg-red-100 text-red-700 border border-red-200 animate-pulse ${className}`.trim()}
      title="Trajectory recording is active"
      aria-label="Recording active"
    >
      <span className="w-2 h-2 bg-red-500 rounded-full" />
      {label}
    </span>
  );
}

export default RecordingIndicator;
