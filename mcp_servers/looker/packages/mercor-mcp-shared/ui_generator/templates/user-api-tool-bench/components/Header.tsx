// Main header component for the API Tool Bench
// Self-contained: imports its own data and opens tool forms in a dialog.

import { useState, useRef, useEffect } from 'react';
import { AuthUser, DataType, dataTypes, getToolEndpoint } from '@/lib/api-config';
import { useServerInfo } from '@mcp-shared-lib/useServerInfo';
import LoginButton from './ui/LoginButton';
import ToolDialog from '@mcp-shared/ToolDialog';

export interface HeaderProps {
  user: AuthUser | null;
  onLogout: () => void;
  onLogin: (token: string, user: AuthUser) => void;
  token: string;
  additionalBadges?: React.ReactNode;
}

export default function Header({
  user,
  onLogout,
  onLogin,
  token,
  additionalBadges,
}: HeaderProps) {
  const [dbDropdownOpen, setDbDropdownOpen] = useState(false);
  const [dialogTool, setDialogTool] = useState<DataType | null>(null);
  const dbDropdownRef = useRef<HTMLDivElement>(null);

  const { serverInfo } = useServerInfo();

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

  const handleLoginClick = () => {
    const loginTool = dataTypes.find(dt => getToolEndpoint(dt) === 'login_tool');
    if (loginTool) {
      setDialogTool(loginTool);
    }
  };

  return (
    <>
      <div className="flex justify-between items-center mb-6">
        <div className="flex items-center gap-3">
          <h1 className="text-3xl font-bold text-gray-900">
            {serverInfo?.name || process.env.NEXT_PUBLIC_SERVER_NAME || 'MCP'} Tools
          </h1>
          {process.env.NEXT_PUBLIC_API_BASE?.includes('127.0.0.1') && (
            <span className="px-2 py-1 text-xs font-medium bg-green-100 text-green-800 rounded-full">
              Local
            </span>
          )}
          {additionalBadges}
        </div>
        <div className="flex items-center gap-4">
          <LoginButton
            user={user}
            dataTypes={dataTypes}
            onLoginClick={handleLoginClick}
            onLogout={onLogout}
          />

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
                        setDialogTool(tool);
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

      <ToolDialog
        dataType={dialogTool}
        onClose={() => setDialogTool(null)}
        token={token}
        onLogin={onLogin}
        onLogout={onLogout}
      />
    </>
  );
}
