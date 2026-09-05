/**
 * TrajectoryPanel - Panel for viewing and managing trajectory sessions.
 *
 * Displays a list of recorded trajectory sessions with the ability to
 * view details, export, and delete sessions.
 *
 * Usage:
 *   <TrajectoryPanel
 *     onSelectSession={(session) => console.log('Selected:', session)}
 *     onClose={() => setShowPanel(false)}
 *   />
 */

import React, { useState, useEffect, useCallback } from 'react';
import { getApiBase } from '@mcp-shared/utils/api';

export interface TrajectoryToolCall {
  id: string;
  tool_name: string;
  input: Record<string, any>;
  output: any;
  timestamp: string;
  duration_ms?: number;
}

export interface TrajectorySession {
  id: string;
  name?: string;
  started_at: string;
  ended_at?: string;
  tool_call_count?: number;
  tool_calls?: TrajectoryToolCall[];
}

export interface TrajectoryPanelProps {
  /** Called when a session is selected for viewing */
  onSelectSession?: (session: TrajectorySession) => void;
  /** Called when the panel should be closed */
  onClose?: () => void;
  /** Optional custom class names */
  className?: string;
}

/**
 * Panel for browsing and managing trajectory sessions.
 */
export function TrajectoryPanel({
  onSelectSession,
  onClose,
  className = '',
}: TrajectoryPanelProps): React.ReactElement {
  const [sessions, setSessions] = useState<TrajectorySession[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedSession, setSelectedSession] = useState<TrajectorySession | null>(null);
  const [expandedSession, setExpandedSession] = useState<string | null>(null);

  // Fetch list of sessions
  const fetchSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const apiBase = getApiBase();
      const response = await fetch(`${apiBase}/trajectory/sessions`);
      if (!response.ok) {
        throw new Error(`Failed to fetch sessions: ${response.status}`);
      }
      const data = await response.json();
      setSessions(data.sessions || data || []);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load sessions';
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  // Fetch session details
  const fetchSessionDetails = useCallback(async (sessionId: string) => {
    try {
      const apiBase = getApiBase();
      const response = await fetch(`${apiBase}/trajectory/session/${sessionId}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch session: ${response.status}`);
      }
      const data = await response.json();
      setSelectedSession(data);
      onSelectSession?.(data);
    } catch (err) {
      console.error('[Trajectory] Error fetching session details:', err);
    }
  }, [onSelectSession]);

  // Export session
  const handleExport = useCallback(async (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const apiBase = getApiBase();
      const response = await fetch(`${apiBase}/trajectory/session/${sessionId}/export`);
      if (!response.ok) {
        throw new Error(`Failed to export: ${response.status}`);
      }
      const data = await response.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `trajectory-${sessionId}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('[Trajectory] Error exporting session:', err);
    }
  }, []);

  // Delete session
  const handleDelete = useCallback(async (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    if (!confirm('Are you sure you want to delete this session?')) {
      return;
    }
    try {
      const apiBase = getApiBase();
      const response = await fetch(`${apiBase}/trajectory/session/${sessionId}`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        throw new Error(`Failed to delete: ${response.status}`);
      }
      setSessions(prev => prev.filter(s => s.id !== sessionId));
      if (selectedSession?.id === sessionId) {
        setSelectedSession(null);
      }
    } catch (err) {
      console.error('[Trajectory] Error deleting session:', err);
    }
  }, [selectedSession]);

  const formatDate = (dateStr: string) => {
    try {
      return new Date(dateStr).toLocaleString();
    } catch {
      return dateStr;
    }
  };

  return (
    <div className={`bg-white rounded-lg shadow-lg border border-gray-200 ${className}`}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
        <h2 className="text-lg font-semibold text-gray-900">Trajectory Sessions</h2>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchSessions}
            className="p-1.5 text-gray-500 hover:text-gray-700 transition-colors"
            title="Refresh"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
          {onClose && (
            <button
              onClick={onClose}
              className="p-1.5 text-gray-500 hover:text-gray-700 transition-colors"
              title="Close"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="p-4 max-h-96 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center py-8">
            <svg className="w-6 h-6 animate-spin text-indigo-600" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
          </div>
        ) : error ? (
          <div className="text-center py-8">
            <p className="text-red-600 text-sm">{error}</p>
            <button
              onClick={fetchSessions}
              className="mt-2 text-sm text-indigo-600 hover:text-indigo-800"
            >
              Try again
            </button>
          </div>
        ) : sessions.length === 0 ? (
          <div className="text-center py-8 text-gray-500">
            <svg className="w-12 h-12 mx-auto mb-3 text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
            </svg>
            <p className="text-sm">No trajectory sessions recorded yet.</p>
            <p className="text-xs mt-1">Start recording to capture tool call sequences.</p>
          </div>
        ) : (
          <div className="space-y-2">
            {sessions.map((session) => (
              <div
                key={session.id}
                className={`border rounded-lg transition-colors cursor-pointer ${
                  expandedSession === session.id
                    ? 'border-indigo-300 bg-indigo-50'
                    : 'border-gray-200 hover:border-gray-300 hover:bg-gray-50'
                }`}
              >
                {/* Session Header */}
                <div
                  className="flex items-center justify-between p-3"
                  onClick={() => {
                    if (expandedSession === session.id) {
                      setExpandedSession(null);
                    } else {
                      setExpandedSession(session.id);
                      fetchSessionDetails(session.id);
                    }
                  }}
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-sm text-gray-900 truncate">
                        {session.name || `Session ${session.id.slice(0, 8)}`}
                      </span>
                      {!session.ended_at && (
                        <span className="px-1.5 py-0.5 text-xs bg-green-100 text-green-700 rounded">
                          Active
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-3 mt-1 text-xs text-gray-500">
                      <span>{formatDate(session.started_at)}</span>
                      {session.tool_call_count !== undefined && (
                        <span>{session.tool_call_count} tool calls</span>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-1 ml-2">
                    <button
                      onClick={(e) => handleExport(session.id, e)}
                      className="p-1.5 text-gray-400 hover:text-indigo-600 transition-colors"
                      title="Export"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                      </svg>
                    </button>
                    <button
                      onClick={(e) => handleDelete(session.id, e)}
                      className="p-1.5 text-gray-400 hover:text-red-600 transition-colors"
                      title="Delete"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                      </svg>
                    </button>
                    <svg
                      className={`w-4 h-4 text-gray-400 transition-transform ${expandedSession === session.id ? 'rotate-180' : ''}`}
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </div>
                </div>

                {/* Expanded Details */}
                {expandedSession === session.id && selectedSession?.id === session.id && (
                  <div className="border-t border-gray-200 p-3 bg-white rounded-b-lg">
                    {selectedSession.tool_calls && selectedSession.tool_calls.length > 0 ? (
                      <div className="space-y-2">
                        <h4 className="text-xs font-medium text-gray-700 uppercase tracking-wide">
                          Tool Calls ({selectedSession.tool_calls.length})
                        </h4>
                        <div className="space-y-1 max-h-48 overflow-y-auto">
                          {selectedSession.tool_calls.map((call, idx) => (
                            <div
                              key={call.id || idx}
                              className="text-xs p-2 bg-gray-50 rounded border border-gray-100"
                            >
                              <div className="flex items-center justify-between">
                                <span className="font-medium text-gray-900">{call.tool_name}</span>
                                {call.duration_ms && (
                                  <span className="text-gray-400">{call.duration_ms}ms</span>
                                )}
                              </div>
                              <div className="mt-1 text-gray-500 truncate">
                                Input: {JSON.stringify(call.input).slice(0, 100)}...
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    ) : (
                      <p className="text-xs text-gray-500">No tool calls recorded in this session.</p>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default TrajectoryPanel;
