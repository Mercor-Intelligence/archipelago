// Main header component for the API Tool Bench
// This component can be customized by projects that need additional features
// (e.g., ONLINE/OFFLINE badges, custom branding)

import { useState, useRef, useEffect } from 'react';
import { AuthUser, DataType } from '@/lib/api-config';
import { useTrajectoryOptional } from '@mcp-shared-lib/TrajectoryContext';
import LoginButton from './ui/LoginButton';

interface HeaderProps {
  user: AuthUser | null;
  onLogout: () => void;
  dataTypes: DataType[];
  onLoginClick: () => void;
  onSelectTool?: (tool: DataType) => void;
  // Optional slot for additional badges/status indicators
  additionalBadges?: React.ReactNode;
}

export default function Header({
  user,
  onLogout,
  dataTypes,
  onLoginClick,
  onSelectTool,
  additionalBadges,
}: HeaderProps) {
  const [dbDropdownOpen, setDbDropdownOpen] = useState(false);
  const [showNameInput, setShowNameInput] = useState(false);
  const [sessionName, setSessionName] = useState('');
  const dbDropdownRef = useRef<HTMLDivElement>(null);

  // Trajectory context (optional - works without provider)
  const trajectory = useTrajectoryOptional();

  const databaseTools = dataTypes.filter(dt => dt.category === 'Database');

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dbDropdownRef.current && !dbDropdownRef.current.contains(event.target as Node)) {
        setDbDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Handle start recording
  const handleStartRecording = async () => {
    if (!trajectory) return;
    await trajectory.startSession(sessionName.trim() || undefined);
    setSessionName('');
    setShowNameInput(false);
  };

  // Render trajectory controls
  const renderTrajectoryControls = () => {
    if (!trajectory) return null;

    const { isRecording, loading, sessionId, stopSession, exportSession } = trajectory;

    if (!isRecording) {
      if (showNameInput) {
        return (
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
        );
      }

      return (
        <button
          onClick={() => setShowNameInput(true)}
          disabled={loading}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border transition-colors
            text-gray-700 bg-white border-gray-300 hover:bg-gray-50 hover:border-gray-400
            disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <svg className="w-3.5 h-3.5 text-red-500" fill="currentColor" viewBox="0 0 20 20">
            <circle cx="10" cy="10" r="6" />
          </svg>
          Record Trajectory
        </button>
      );
    }

    // Recording active
    return (
      <div className="flex items-center gap-2">
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
          onClick={stopSession}
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
          onClick={exportSession}
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
      </div>
    );
  };

  return (
    <div className="flex justify-between items-center mb-6">
      <div className="flex items-center gap-3">
        <h1 className="text-3xl font-bold text-gray-900">
          {process.env.NEXT_PUBLIC_SERVER_NAME || 'MCP'} Tools
        </h1>
        {process.env.NEXT_PUBLIC_API_BASE?.includes('127.0.0.1') && (
          <span className="px-2 py-1 text-xs font-medium bg-green-100 text-green-800 rounded-full">
            Local
          </span>
        )}
        {/* Slot for additional badges (e.g., ONLINE/OFFLINE status) */}
        {additionalBadges}
      </div>
      <div className="flex items-center gap-4">
        {/* Trajectory recording controls */}
        {renderTrajectoryControls()}

        {/* Login/User section - handles auth detection via server_info tool */}
        <LoginButton
          user={user}
          dataTypes={dataTypes}
          onLoginClick={onLoginClick}
          onLogout={onLogout}
        />

        {/* Database tools dropdown */}
        {databaseTools.length > 0 && (
          <div className="relative" ref={dbDropdownRef}>
            <button
              onClick={() => setDbDropdownOpen(!dbDropdownOpen)}
              className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
              </svg>
              Database
              <svg className={`w-4 h-4 transition-transform ${dbDropdownOpen ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
              </svg>
            </button>
            {dbDropdownOpen && (
              <div className="absolute right-0 mt-2 w-48 bg-white rounded-md shadow-lg border border-gray-200 py-1 z-50">
                {databaseTools.map(tool => (
                  <button
                    key={tool.name}
                    onClick={() => {
                      onSelectTool?.(tool);
                      setDbDropdownOpen(false);
                    }}
                    className="w-full text-left px-4 py-2 text-sm text-gray-700 hover:bg-gray-100"
                  >
                    {tool.displayName || tool.name}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export type { HeaderProps };
