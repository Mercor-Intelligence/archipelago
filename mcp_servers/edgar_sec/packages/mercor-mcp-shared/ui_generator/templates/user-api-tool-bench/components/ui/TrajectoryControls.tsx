/**
 * TrajectoryControls - Recording controls for trajectory capture.
 *
 * Provides UI controls for starting/stopping trajectory recording sessions
 * and exporting recorded tool call sequences.
 *
 * Uses TrajectoryContext for state management and API calls.
 *
 * Usage:
 *   // Within a TrajectoryProvider:
 *   <TrajectoryControls />
 *   <TrajectoryControls variant="compact" />
 */

import React, { useState, useCallback } from 'react';
import { useTrajectory, TrajectorySession } from '@mcp-shared-lib/TrajectoryContext';

export type { TrajectorySession };

export interface TrajectoryToolCall {
  id: string;
  tool_name: string;
  input: Record<string, any>;
  output: any;
  timestamp: string;
  duration_ms?: number;
}

export interface TrajectoryControlsProps {
  /** Optional custom class names */
  className?: string;
  /** Variant: 'full' shows all controls, 'compact' shows minimal UI */
  variant?: 'full' | 'compact';
}

/**
 * Controls for managing trajectory recording sessions.
 * Must be used within a TrajectoryProvider.
 */
export function TrajectoryControls({
  className = '',
  variant = 'full',
}: TrajectoryControlsProps): React.ReactElement {
  const {
    sessionId,
    isRecording,
    loading,
    error,
    startSession,
    stopSession,
    exportSession,
  } = useTrajectory();

  const [sessionName, setSessionName] = useState('');
  const [showNameInput, setShowNameInput] = useState(false);

  // Start a new recording session
  const handleStartRecording = useCallback(async () => {
    const newSessionId = await startSession(sessionName.trim() || undefined);
    if (newSessionId) {
      setSessionName('');
      setShowNameInput(false);
    }
  }, [sessionName, startSession]);

  // Stop recording
  const handleStopRecording = useCallback(async () => {
    await stopSession();
  }, [stopSession]);

  // Export session data
  const handleExport = useCallback(async () => {
    await exportSession();
  }, [exportSession]);

  // Compact variant - just show recording indicator with stop button
  if (variant === 'compact') {
    if (!isRecording) {
      return (
        <button
          onClick={() => setShowNameInput(true)}
          disabled={loading}
          className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-full border transition-colors
            text-gray-600 bg-white border-gray-300 hover:bg-gray-50 hover:border-gray-400
            disabled:opacity-50 disabled:cursor-not-allowed ${className}`}
          title="Start recording trajectory"
        >
          <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20">
            <circle cx="10" cy="10" r="6" />
          </svg>
          Record
        </button>
      );
    }

    return (
      <div className={`inline-flex items-center gap-2 ${className}`}>
        <span
          className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-full bg-red-100 text-red-700 border border-red-200 animate-pulse"
          title={`Recording: ${sessionId}`}
        >
          <span className="w-2 h-2 bg-red-500 rounded-full" />
          REC
        </span>
        <button
          onClick={handleStopRecording}
          disabled={loading}
          className="p-1 text-gray-500 hover:text-red-600 transition-colors disabled:opacity-50"
          title="Stop recording"
        >
          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
            <rect x="5" y="5" width="10" height="10" rx="1" />
          </svg>
        </button>
        <button
          onClick={handleExport}
          disabled={loading}
          className="p-1 text-gray-500 hover:text-indigo-600 transition-colors disabled:opacity-50"
          title="Export recording"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
          </svg>
        </button>
      </div>
    );
  }

  // Full variant
  return (
    <div className={`flex items-center gap-3 ${className}`}>
      {error && (
        <span className="text-xs text-red-600" title={error}>
          Error
        </span>
      )}

      {!isRecording ? (
        showNameInput ? (
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={sessionName}
              onChange={(e) => setSessionName(e.target.value)}
              placeholder="Session name (optional)"
              className="px-2 py-1 text-sm border border-gray-300 rounded focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 outline-none"
              onKeyDown={(e) => e.key === 'Enter' && handleStartRecording()}
              autoFocus
            />
            <button
              onClick={handleStartRecording}
              disabled={loading}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md
                bg-red-600 text-white hover:bg-red-700 transition-colors
                disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loading ? (
                <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              ) : (
                <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20">
                  <circle cx="10" cy="10" r="6" />
                </svg>
              )}
              Start
            </button>
            <button
              onClick={() => { setShowNameInput(false); setSessionName(''); }}
              className="p-1 text-gray-400 hover:text-gray-600"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        ) : (
          <button
            onClick={() => setShowNameInput(true)}
            disabled={loading}
            className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border transition-colors
              text-gray-700 bg-white border-gray-300 hover:bg-gray-50 hover:border-gray-400
              disabled:opacity-50 disabled:cursor-not-allowed`}
          >
            <svg className="w-3.5 h-3.5 text-red-500" fill="currentColor" viewBox="0 0 20 20">
              <circle cx="10" cy="10" r="6" />
            </svg>
            Record Trajectory
          </button>
        )
      ) : (
        <>
          {/* Recording indicator */}
          <span
            className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-full bg-red-100 text-red-700 border border-red-200 animate-pulse"
            title={`Session: ${sessionId}`}
          >
            <span className="w-2 h-2 bg-red-500 rounded-full" />
            Recording
          </span>

          {/* Stop button */}
          <button
            onClick={handleStopRecording}
            disabled={loading}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md
              text-gray-700 bg-white border border-gray-300 hover:bg-gray-50
              disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20">
              <rect x="5" y="5" width="10" height="10" rx="1" />
            </svg>
            Stop
          </button>

          {/* Export button */}
          <button
            onClick={handleExport}
            disabled={loading}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md
              text-indigo-700 bg-indigo-50 border border-indigo-200 hover:bg-indigo-100
              disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
            </svg>
            Export
          </button>
        </>
      )}
    </div>
  );
}

export default TrajectoryControls;
