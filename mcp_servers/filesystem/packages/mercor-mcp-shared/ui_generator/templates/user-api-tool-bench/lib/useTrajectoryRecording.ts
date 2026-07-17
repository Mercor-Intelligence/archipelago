/**
 * Hook for trajectory recording support in MCP UIs.
 *
 * Trajectory recording captures tool call sequences for ML training/evaluation.
 * When a trajectory_session parameter is passed in the URL, all API calls
 * will include this session ID so the server can record the interactions.
 *
 * Usage:
 *   const { trajectorySessionId, buildApiUrl, isRecording } = useTrajectoryRecording();
 *
 *   // Build API URL with trajectory session if recording
 *   const url = buildApiUrl('/tools/my_tool');
 *   await fetch(url, { method: 'POST', body: JSON.stringify(args) });
 */

import { useState, useEffect, useCallback } from 'react';

export interface TrajectoryRecordingState {
  /** The trajectory session ID if recording is active, null otherwise */
  trajectorySessionId: string | null;
  /** Whether recording is currently active */
  isRecording: boolean;
  /** Whether the hook has finished checking for trajectory params (safe to make API calls) */
  isReady: boolean;
  /**
   * Build an API URL that includes the trajectory session parameter if recording.
   * @param endpoint - The API endpoint (e.g., '/tools/my_tool')
   * @param apiBase - Optional API base URL (defaults to same-origin '')
   * @returns The full URL with trajectory session parameter if recording
   */
  buildApiUrl: (endpoint: string, apiBase?: string) => string;
}

/**
 * Hook to manage trajectory recording state.
 * Reads the trajectory_session parameter from URL on mount.
 */
export function useTrajectoryRecording(): TrajectoryRecordingState {
  const [trajectorySessionId, setTrajectorySessionId] = useState<string | null>(null);
  const [isReady, setIsReady] = useState(false);

  // Check for trajectory session ID from URL params on mount
  useEffect(() => {
    if (typeof window !== 'undefined') {
      const urlParams = new URLSearchParams(window.location.search);
      const trajectorySession = urlParams.get('trajectory_session');
      if (trajectorySession) {
        console.log('[Trajectory] Recording session active:', trajectorySession);
        setTrajectorySessionId(trajectorySession);
      }
      setIsReady(true);
    }
  }, []);

  // Helper to build API URL with trajectory session parameter
  const buildApiUrl = useCallback((endpoint: string, apiBase: string = ''): string => {
    const base = `${apiBase}${endpoint}`;
    if (trajectorySessionId) {
      const separator = endpoint.includes('?') ? '&' : '?';
      return `${base}${separator}trajectory_session=${trajectorySessionId}`;
    }
    return base;
  }, [trajectorySessionId]);

  return {
    trajectorySessionId,
    isRecording: trajectorySessionId !== null,
    isReady,
    buildApiUrl,
  };
}

export default useTrajectoryRecording;
