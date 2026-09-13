import { AuthUser, dataTypes, getToolEndpoint } from '@/lib/api-config';

interface UserMenuProps {
  user: AuthUser | null;
  onLogout: () => void;
  onLoginClick: () => void;
}

export default function UserMenu({ user, onLogout, onLoginClick }: UserMenuProps) {
  if (user) {
    return (
      <div className="flex items-center gap-4">
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
      </div>
    );
  }

  const hasLoginTool = dataTypes.some(dt => getToolEndpoint(dt) === 'login_tool');
  const hasAuthRequiredTools = dataTypes.some(dt => dt._internal?.requiresAuth);

  // Warning: tools require auth but no login_tool exists
  if (!hasLoginTool && hasAuthRequiredTools) {
    return (
      <div className="flex items-center gap-2 px-4 py-2 text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-md">
        <svg className="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
        </svg>
        <span className="font-medium">Configuration Issue:</span>
        <span>Tools require authentication but no login_tool is configured</span>
      </div>
    );
  }

  // Show login button if login_tool exists
  if (hasLoginTool) {
    return (
      <button
        onClick={onLoginClick}
        className="flex items-center gap-2 px-4 py-2 text-sm font-medium text-indigo-700 bg-indigo-50 border border-indigo-200 rounded-md hover:bg-indigo-100 transition-colors"
      >
        <span className="inline-block w-2 h-2 rounded-full bg-yellow-400"></span>
        Login
      </button>
    );
  }

  return null;
}
