// Main page component - can be overridden via components/overrides/MainPage.tsx
import { AuthUser } from '@/lib/api-config';
import { TrajectoryProvider } from '@mcp-shared-lib/TrajectoryContext';
import ApiTool from '@mcp-shared/ApiTool';
import Head from 'next/head';
import { useEffect, useState } from 'react';

export interface MainPageProps {
  /** The app/project name, used for page title and auth storage key */
  appName: string;
  /** Optional custom page title (defaults to "MCP Tools") */
  pageTitle?: string;
  /** Optional custom page description */
  pageDescription?: string;
}

/**
 * Default main page component that renders the ApiTool interface.
 *
 * This component can be overridden by creating a file at:
 * components/overrides/MainPage.tsx
 *
 * When overriding, you can:
 * - Extend this component using @mcp-shared-base/MainPage
 * - Replace it entirely with a custom implementation
 *
 * Example override that adds a custom header:
 * ```tsx
 * import BaseMainPage, { MainPageProps } from '@mcp-shared-base/MainPage';
 *
 * export default function MainPage(props: MainPageProps) {
 *   return (
 *     <div>
 *       <div className="bg-blue-500 p-4 text-white">Custom Header</div>
 *       <BaseMainPage {...props} />
 *     </div>
 *   );
 * }
 * ```
 */
export default function MainPage({
  appName,
  pageTitle = 'MCP Tools',
  pageDescription = 'Explore and use MCP tools'
}: MainPageProps) {
  // Derive storage key from app name (e.g., "edgar_sec" -> "edgar_sec_auth")
  const authStorageKey = `${appName.toLowerCase().replace(/\s+/g, '_')}_auth`;

  const [authState, setAuthState] = useState<{ token: string | null; user: AuthUser | null }>({
    token: null,
    user: null
  });

  // Load auth state from localStorage on mount
  useEffect(() => {
    const savedAuth = localStorage.getItem(authStorageKey);
    if (savedAuth) {
      try {
        const parsed = JSON.parse(savedAuth);
        setAuthState(parsed);
      } catch (e) {
        localStorage.removeItem(authStorageKey);
      }
    }
  }, [authStorageKey]);

  // Handle login - called when login_tool returns a successful response
  const handleLogin = (token: string, user: AuthUser) => {
    const newAuthState = { token, user };
    setAuthState(newAuthState);
    localStorage.setItem(authStorageKey, JSON.stringify(newAuthState));
  };

  // Handle logout
  const handleLogout = () => {
    setAuthState({ token: null, user: null });
    localStorage.removeItem(authStorageKey);
  };

  return (
    <TrajectoryProvider>
      <Head>
        <title>{pageTitle}</title>
        <meta name="description" content={pageDescription} />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link rel="icon" href="/favicon.ico" />
      </Head>
      <div className="h-screen flex flex-col overflow-hidden">
        <main className="flex-1 overflow-auto">
          <ApiTool
            token={authState.token || ''}
            user={authState.user}
            onLogout={handleLogout}
            onLogin={handleLogin}
          />
        </main>
      </div>
    </TrajectoryProvider>
  );
}

export type { AuthUser };
