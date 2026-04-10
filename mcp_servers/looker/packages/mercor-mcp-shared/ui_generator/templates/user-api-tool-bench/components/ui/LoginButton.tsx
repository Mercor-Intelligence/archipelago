// Login button component that detects auth requirements via server_info tool
// Uses the shared useServerInfo hook (singleton cache, no duplicate fetches)

import { AuthUser, DataType, getToolEndpoint } from '@/lib/api-config';
import { useServerInfo } from '@mcp-shared-lib/useServerInfo';

interface LoginButtonProps {
  user: AuthUser | null;
  dataTypes: DataType[];
  onLoginClick: () => void;
  onLogout: () => void;
}

export default function LoginButton({
  user,
  dataTypes,
  onLoginClick,
  onLogout,
}: LoginButtonProps) {
  const { serverInfo, loading } = useServerInfo();

  const hasLoginTool = dataTypes.some(dt => getToolEndpoint(dt) === 'login_tool');
  const hasAuthRequiredTools = dataTypes.some(dt => dt._internal?.requiresAuth);

  const authEnabled = serverInfo?.features?.authentication ?? null;

  // Still loading or auth disabled - render nothing
  if (loading || authEnabled === null || authEnabled === false) {
    return null;
  }

  // Logged in user - show user info and logout button
  if (user && authEnabled) {
    return (
      <>
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-full bg-indigo-600 flex items-center justify-center text-white font-medium text-sm">
            {user.username.charAt(0).toUpperCase()}
          </div>
          <div className="text-sm">
            <div className="font-medium text-gray-900">{user.username}</div>
            <div className="text-xs text-gray-500">
              {user.roles.map(role => (
                <span key={role} className="inline-block px-1.5 py-0.5 bg-indigo-100 text-indigo-700 rounded mr-1">
                  {role}
                </span>
              ))}
            </div>
          </div>
        </div>
        <button
          onClick={onLogout}
          className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50"
        >
          Logout
        </button>
      </>
    );
  }

  // Not logged in - show warning or login button based on auth config
  return (
    <>
      {/* Warning: tools require auth but no login_tool exists */}
      {authEnabled && !hasLoginTool && hasAuthRequiredTools && (
        <div className="flex items-center gap-2 px-4 py-2 text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-md">
          <svg className="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
          <span className="font-medium">Configuration Issue:</span>
          <span>Tools require authentication but no login_tool is configured</span>
        </div>
      )}

      {/* Show login button if auth is enabled and login_tool exists */}
      {authEnabled && hasLoginTool && (
        <button
          onClick={onLoginClick}
          className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-indigo-700 bg-indigo-50 border border-indigo-200 rounded-md hover:bg-indigo-100 transition-colors"
        >
          <span className="inline-block w-2 h-2 rounded-full bg-yellow-400"></span>
          Login
        </button>
      )}
    </>
  );
}

export type { LoginButtonProps };
