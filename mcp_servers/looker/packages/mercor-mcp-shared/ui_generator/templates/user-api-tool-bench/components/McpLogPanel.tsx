// MCP Activity Log Panel - shows all MCP tool calls.
// Recording is handled by the server-side trajectory system.
import { useState, useEffect } from 'react';
import { mcpLog, McpLogEntry, initToolLabels, generateActivityPdf, PdfExportEntry } from '@mcp-shared-lib/mcp-log';
import { useTrajectoryOptional } from '@mcp-shared-lib/TrajectoryContext';
import { dataTypes } from '@/lib/api-config';

// Session name modal component
function SessionNameModal({
  isOpen,
  onClose,
  onStart,
}: {
  isOpen: boolean;
  onClose: () => void;
  onStart: (sessionName: string) => void;
}) {
  const [sessionName, setSessionName] = useState('');

  useEffect(() => {
    if (isOpen) {
      const now = new Date();
      const pad = (n: number) => String(n).padStart(2, '0');
      const formatted = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}_${pad(now.getHours())}-${pad(now.getMinutes())}`;
      setSessionName(`recording_${formatted}`);
    }
  }, [isOpen]);

  if (!isOpen) return null;

  const handleStart = () => {
    onStart(sessionName.trim() || `Recording - ${new Date().toLocaleString()}`);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50">
      <div className="bg-white rounded-lg shadow-xl w-96 overflow-hidden">
        <div className="px-6 py-4 border-b border-gray-200">
          <h3 className="text-lg font-semibold text-gray-900">Start Recording Session</h3>
          <p className="text-sm text-gray-500 mt-1">
            Name this session for the exported report
          </p>
        </div>
        <div className="px-6 py-4">
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Session Name
          </label>
          <input
            type="text"
            value={sessionName}
            onChange={(e) => setSessionName(e.target.value)}
            placeholder="Name session related to task for reviewer"
            className="w-full px-3 py-2 border border-gray-300 rounded-md text-gray-900 focus:outline-none focus:ring-2 focus:ring-red-500 focus:border-red-500"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleStart();
              if (e.key === 'Escape') onClose();
            }}
          />
          <p className="text-xs text-gray-500 mt-2">
            Tool calls will be recorded on the server for this session.
          </p>
        </div>
        <div className="px-6 py-4 bg-gray-50 flex justify-end gap-3">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-gray-700 hover:text-gray-900"
          >
            Cancel
          </button>
          <button
            onClick={handleStart}
            className="px-4 py-2 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded-md flex items-center gap-2"
          >
            <span className="w-2 h-2 rounded-full bg-white animate-pulse" />
            Start Recording
          </button>
        </div>
      </div>
    </div>
  );
}

export default function McpLogPanel() {
  const [entries, setEntries] = useState<McpLogEntry[]>([]);
  const [isExpanded, setIsExpanded] = useState(true);
  const [expandedEntries, setExpandedEntries] = useState<Set<string>>(new Set());
  const [showSessionModal, setShowSessionModal] = useState(false);
  const [lastSessionId, setLastSessionId] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  // Trajectory context for server-side recording
  const trajectory = useTrajectoryOptional();
  const isRecording = trajectory?.isRecording ?? false;
  const trajectoryLoading = trajectory?.loading ?? false;

  useEffect(() => {
    // Initialize tool labels from app config
    initToolLabels(dataTypes);

    // Initial load
    setEntries(mcpLog.getEntries());

    // Subscribe to updates
    const unsubscribe = mcpLog.subscribe((newEntries) => {
      setEntries([...newEntries]);
    });

    return () => {
      unsubscribe();
    };
  }, []);

  const handleRecordClick = async () => {
    if (isRecording) {
      // Capture session ID before stopping - stopSession clears it immediately
      const currentSessionId = trajectory?.sessionId ?? null;
      await trajectory?.stopSession();
      setLastSessionId(currentSessionId);
    } else {
      setShowSessionModal(true);
    }
  };

  const handleStartRecording = async (name: string) => {
    mcpLog.clearEntries();
    await trajectory?.startSession(name);
  };

  const handleExportPdf = async () => {
    if (!trajectory || !lastSessionId) return;
    setExporting(true);
    try {
      const sessionData = await trajectory.fetchSessionData(lastSessionId);
      if (!sessionData?.tool_calls?.length) return;

      const pdfEntries: PdfExportEntry[] = sessionData.tool_calls.map((call: any) => ({
        tool: call.tool_name,
        parameters: call.arguments || {},
        response: call.response,
        status: call.success ? 'success' as const : 'error' as const,
        timestamp: call.timestamp,
        duration: call.duration_ms,
      }));

      generateActivityPdf(pdfEntries, sessionData.session_id);
    } finally {
      setExporting(false);
    }
  };

  const handleExportJson = async () => {
    if (!trajectory) return;
    // Export active session or last stopped session
    if (isRecording) {
      await trajectory.exportSession();
    } else if (lastSessionId) {
      const data = await trajectory.fetchSessionData(lastSessionId);
      if (!data) return;
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `trajectory-${lastSessionId}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }
  };

  const toggleEntry = (id: string) => {
    setExpandedEntries(prev => {
      const newSet = new Set(prev);
      if (newSet.has(id)) {
        newSet.delete(id);
      } else {
        newSet.add(id);
      }
      return newSet;
    });
  };

  const formatTime = (date: Date) => {
    return date.toLocaleTimeString('en-US', {
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  };

  const formatParams = (params: Record<string, any>) => {
    if (!params || Object.keys(params).length === 0) {
      return '{}';
    }
    return JSON.stringify(params, null, 2);
  };

  const getStatusColor = (status: McpLogEntry['status']) => {
    switch (status) {
      case 'pending':
        return 'text-yellow-600 bg-yellow-100';
      case 'success':
        return 'text-green-600 bg-green-100';
      case 'error':
        return 'text-red-600 bg-red-100';
    }
  };

  const getStatusIcon = (status: McpLogEntry['status']) => {
    switch (status) {
      case 'pending':
        return (
          <svg className="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
          </svg>
        );
      case 'success':
        return (
          <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
        );
      case 'error':
        return (
          <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        );
    }
  };

  // Show export buttons when we have a completed session
  const canExport = !isRecording && lastSessionId !== null;

  return (
    <>
      <div className={`fixed bottom-0 right-0 w-96 bg-gray-900 text-white shadow-2xl rounded-tl-lg z-50 flex flex-col max-h-[50vh] transition-all ${
        isRecording ? 'ring-2 ring-red-500 ring-offset-2 ring-offset-gray-900' : ''
      }`}>
        {/* Header */}
        <div
          className={`flex items-center justify-between px-4 py-2 rounded-tl-lg cursor-pointer ${
            isRecording ? 'bg-red-900/50' : 'bg-gray-800'
          }`}
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <div className="flex items-center gap-2">
            <svg className="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
            <span className="text-sm font-medium">MCP Activity Log</span>
            {/* Record/Stop button */}
            {trajectory && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  handleRecordClick();
                }}
                disabled={trajectoryLoading}
                className={`flex items-center gap-1 text-xs px-2 py-1 rounded transition-colors ${
                  isRecording
                    ? 'bg-red-600 text-white hover:bg-red-700'
                    : 'text-gray-400 hover:text-white hover:bg-gray-700'
                } disabled:opacity-50`}
                title={isRecording ? 'Stop recording' : 'Start recording session'}
              >
                <span className={`w-2 h-2 rounded-full ${isRecording ? 'bg-white animate-pulse' : 'bg-red-500'}`} />
                {isRecording ? 'Stop' : 'Record'}
              </button>
            )}
            <span className="text-xs text-gray-400">({entries.length})</span>
          </div>
          <div className="flex items-center gap-2">
            {/* Export buttons - show when we have a completed session */}
            {canExport && (
              <>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleExportPdf();
                  }}
                  disabled={exporting}
                  className="flex items-center gap-1 text-xs text-green-400 hover:text-green-300 px-2 py-1 rounded hover:bg-gray-700 disabled:opacity-50"
                  title="Export session as PDF report"
                >
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
                  </svg>
                  {exporting ? 'Exporting...' : 'PDF'}
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleExportJson();
                  }}
                  className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300 px-2 py-1 rounded hover:bg-gray-700"
                  title="Export session as JSON"
                >
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                  </svg>
                  JSON
                </button>
              </>
            )}
            {entries.length > 0 && !isRecording && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  mcpLog.clearEntries();
                }}
                className="text-xs text-gray-400 hover:text-white px-2 py-1 rounded hover:bg-gray-700"
              >
                Clear
              </button>
            )}
            <svg
              className={`w-4 h-4 transition-transform ${isExpanded ? 'rotate-180' : ''}`}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
            </svg>
          </div>
        </div>

        {/* Session banner - show when recording */}
        {isRecording && trajectory?.sessionId && (
          <div className="px-4 py-2 bg-red-900/30 border-b border-red-800/50 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
            <span className="text-xs text-red-200 font-medium truncate">{trajectory.sessionId}</span>
            <span className="text-xs text-red-400 ml-auto">{entries.length} calls</span>
          </div>
        )}

        {/* Log entries */}
        {isExpanded && (
          <div className="flex-1 overflow-auto">
            {entries.length === 0 ? (
              <div className="p-4 text-center text-gray-500 text-sm">
                {isRecording ? 'Recording... Perform actions to capture tool calls' : 'No MCP tool calls yet'}
              </div>
            ) : (
              <div className="divide-y divide-gray-800">
                {entries.map((entry) => (
                  <div key={entry.id} className="text-xs">
                    {/* Entry header */}
                    <div
                      className="flex items-center gap-2 px-3 py-2 hover:bg-gray-800 cursor-pointer"
                      onClick={() => toggleEntry(entry.id)}
                    >
                      <span className={`flex items-center justify-center w-5 h-5 rounded-full ${getStatusColor(entry.status)}`}>
                        {getStatusIcon(entry.status)}
                      </span>
                      <span className="text-gray-400 font-mono">{formatTime(entry.timestamp)}</span>
                      <span className="text-blue-400 font-medium flex-1 truncate">{entry.tool}</span>
                      {entry.duration && (
                        <span className="text-gray-500">{entry.duration}ms</span>
                      )}
                      <svg
                        className={`w-3 h-3 text-gray-500 transition-transform ${expandedEntries.has(entry.id) ? 'rotate-90' : ''}`}
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                      >
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                      </svg>
                    </div>

                    {/* Entry details */}
                    {expandedEntries.has(entry.id) && (
                      <div className="px-3 pb-2 pl-10">
                        <div className="bg-gray-800 rounded p-2 font-mono text-[10px] overflow-x-auto max-h-64 overflow-y-auto">
                          <div className="text-gray-400 mb-1">Parameters:</div>
                          <pre className="text-green-400 whitespace-pre-wrap break-all">
                            {formatParams(entry.parameters)}
                          </pre>
                          {entry.response && (
                            <>
                              <div className="text-gray-400 mt-2 mb-1 flex items-center gap-2">
                                Response:
                                {isRecording && (
                                  <span className="text-red-400 text-[9px]">(recording)</span>
                                )}
                              </div>
                              <pre className="text-blue-400 whitespace-pre-wrap break-all max-h-32 overflow-y-auto">
                                {JSON.stringify(entry.response, null, 2)}
                              </pre>
                            </>
                          )}
                          {entry.error && (
                            <>
                              <div className="text-gray-400 mt-2 mb-1">Error:</div>
                              <pre className="text-red-400 whitespace-pre-wrap break-all">
                                {entry.error}
                              </pre>
                            </>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Session Name Modal */}
      <SessionNameModal
        isOpen={showSessionModal}
        onClose={() => setShowSessionModal(false)}
        onStart={handleStartRecording}
      />
    </>
  );
}
