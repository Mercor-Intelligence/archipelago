/**
 * TrajectoryContext - Shared context for trajectory recording state.
 *
 * Provides trajectory session state to any component in the tree.
 * Used by Header (for controls) and ApiTool (for including session ID in calls).
 *
 * Usage:
 *   // In MainPage or App:
 *   <TrajectoryProvider>
 *     <YourApp />
 *   </TrajectoryProvider>
 *
 *   // In any component:
 *   const { sessionId, isRecording, startSession, stopSession } = useTrajectory();
 */

import React, { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { getApiBase } from '@mcp-shared/utils/api';

export interface TrajectorySession {
  id: string;
  name?: string;
  started_at: string;
  ended_at?: string;
}

export interface TrajectoryContextValue {
  /** Current active session ID, null if not recording */
  sessionId: string | null;
  /** Whether recording is currently active */
  isRecording: boolean;
  /** Whether the context is ready (checked URL params) */
  isReady: boolean;
  /** Loading state for async operations */
  loading: boolean;
  /** Error message if any operation failed */
  error: string | null;
  /** Start a new recording session */
  startSession: (name?: string) => Promise<string | null>;
  /** Stop the current recording session */
  stopSession: () => Promise<void>;
  /** Export the current session as JSON */
  exportSession: () => Promise<TrajectorySession | null>;
  /** Clear any error state */
  clearError: () => void;
}

const TrajectoryContext = createContext<TrajectoryContextValue | null>(null);

export interface TrajectoryProviderProps {
  children: React.ReactNode;
}

/**
 * Provider component for trajectory recording state.
 * Wrap your app with this to enable trajectory recording.
 */
export function TrajectoryProvider({ children }: TrajectoryProviderProps): React.ReactElement {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [isReady, setIsReady] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Check for trajectory session in URL params on mount
  useEffect(() => {
    if (typeof window !== 'undefined') {
      const urlParams = new URLSearchParams(window.location.search);
      const trajectorySession = urlParams.get('trajectory_session');
      if (trajectorySession) {
        console.log('[Trajectory] Resuming session from URL:', trajectorySession);
        setSessionId(trajectorySession);
      }
      setIsReady(true);
    }
  }, []);

  // Update URL when session changes
  const updateUrl = useCallback((newSessionId: string | null) => {
    if (typeof window === 'undefined') return;
    const currentUrl = new URL(window.location.href);
    if (newSessionId) {
      currentUrl.searchParams.set('trajectory_session', newSessionId);
    } else {
      currentUrl.searchParams.delete('trajectory_session');
    }
    window.history.replaceState({}, '', currentUrl.toString());
  }, []);

  // Start a new recording session
  const startSession = useCallback(async (name?: string): Promise<string | null> => {
    setLoading(true);
    setError(null);
    try {
      const apiBase = getApiBase();
      const params = new URLSearchParams();
      if (name?.trim()) {
        params.set('session_id', name.trim());
      }
      const url = `${apiBase}/trajectory/start${params.toString() ? '?' + params.toString() : ''}`;

      const response = await fetch(url, { method: 'POST' });
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || `Failed to start session: ${response.status}`);
      }

      const data = await response.json();
      const newSessionId = data.session_id || data.id;

      if (newSessionId) {
        console.log('[Trajectory] Started recording session:', newSessionId);
        setSessionId(newSessionId);
        updateUrl(newSessionId);
        return newSessionId;
      }
      return null;
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to start recording';
      setError(message);
      console.error('[Trajectory] Error starting session:', err);
      return null;
    } finally {
      setLoading(false);
    }
  }, [updateUrl]);

  // Stop recording
  const stopSession = useCallback(async (): Promise<void> => {
    if (!sessionId) return;

    setLoading(true);
    setError(null);
    try {
      const apiBase = getApiBase();
      const response = await fetch(`${apiBase}/trajectory/stop/${sessionId}`, {
        method: 'POST',
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || `Failed to stop session: ${response.status}`);
      }

      console.log('[Trajectory] Stopped recording session:', sessionId);
      setSessionId(null);
      updateUrl(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to stop recording';
      setError(message);
      console.error('[Trajectory] Error stopping session:', err);
    } finally {
      setLoading(false);
    }
  }, [sessionId, updateUrl]);

  // Export session data
  const exportSession = useCallback(async (): Promise<TrajectorySession | null> => {
    if (!sessionId) return null;

    setLoading(true);
    setError(null);
    try {
      const apiBase = getApiBase();
      const response = await fetch(`${apiBase}/trajectory/session/${sessionId}`);

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        throw new Error(errData.detail || `Failed to export session: ${response.status}`);
      }

      const data = await response.json();
      console.log('[Trajectory] Exported session:', sessionId);

      // Download as JSON file
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `trajectory-${sessionId}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      return data;
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to export session';
      setError(message);
      console.error('[Trajectory] Error exporting session:', err);
      return null;
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  const clearError = useCallback(() => setError(null), []);

  const value: TrajectoryContextValue = {
    sessionId,
    isRecording: sessionId !== null,
    isReady,
    loading,
    error,
    startSession,
    stopSession,
    exportSession,
    clearError,
  };

  return (
    <TrajectoryContext.Provider value={value}>
      {children}
    </TrajectoryContext.Provider>
  );
}

/**
 * Hook to access trajectory recording state and methods.
 * Must be used within a TrajectoryProvider.
 */
export function useTrajectory(): TrajectoryContextValue {
  const context = useContext(TrajectoryContext);
  if (!context) {
    throw new Error('useTrajectory must be used within a TrajectoryProvider');
  }
  return context;
}

/**
 * Hook that returns trajectory context if available, or null if not wrapped in provider.
 * Useful for components that optionally support trajectory recording.
 */
export function useTrajectoryOptional(): TrajectoryContextValue | null {
  return useContext(TrajectoryContext);
}

export default TrajectoryContext;
