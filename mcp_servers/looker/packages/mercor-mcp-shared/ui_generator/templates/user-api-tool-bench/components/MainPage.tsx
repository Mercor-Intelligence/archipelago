// Main page component - renders dark tab bar, Header, and tab content.
// Apps can add custom tabs via extraTabs prop without overriding.
// Can still be fully overridden via components/overrides/MainPage.tsx
import { AuthUser, models } from '@/lib/api-config';
import { TrajectoryProvider } from '@mcp-shared-lib/TrajectoryContext';
import { getModeBadgeStyle, useServerInfo } from '@mcp-shared-lib/useServerInfo';
import ApiTool from '@mcp-shared/ApiTool';
import DocsViewer, { useDocsAvailable } from '@mcp-shared/DocsViewer';
import Header from '@mcp-shared/Header';
import ModelsTab from '@mcp-shared/ModelsTab';
import { getBasePath, setAuthToken, setOnAuthError } from '@mcp-shared/utils/api';
import Head from 'next/head';
import Link from 'next/link';
import React, { useCallback, useEffect, useState } from 'react';

// Check if the data-generator page exists (only generated when config/app-config.ts is present)
function useDataWorkflowAvailable(basePath: string): boolean {
  const [available, setAvailable] = useState(false);
  useEffect(() => {
    fetch(`${basePath}/data-generator`, { method: 'HEAD' })
      .then(res => setAvailable(res.ok))
      .catch(() => setAvailable(false));
  }, [basePath]);
  return available;
}

export interface CustomTab {
  id: string;
  label: string;
  content: React.ReactNode;
  visible?: boolean;
  onSelect?: () => void;
}

export interface TabBarStyle {
  bar?: string;
  active?: string;
  inactive?: string;
  label?: string;
}

const defaultTabBarStyle: Required<TabBarStyle> = {
  bar: 'bg-gray-900 border-b border-gray-800',
  active: 'bg-white text-gray-900',
  inactive: 'text-gray-300 hover:text-white hover:bg-gray-800',
  label: 'text-gray-400 text-sm',
};

export interface MainPageProps {
  appName: string;
  pageTitle?: string;
  pageDescription?: string;
  extraTabs?: CustomTab[];
  defaultTab?: string;
  tabBarRight?: React.ReactNode;
  tabBarStyle?: TabBarStyle;
  footer?: React.ReactNode;
}

export default function MainPage({
  appName,
  pageTitle = 'MCP Tools',
  pageDescription = 'Explore and use MCP tools',
  extraTabs = [],
  defaultTab,
  tabBarRight,
  tabBarStyle: tabBarStyleProp,
  footer,
}: MainPageProps) {
  const { serverInfo } = useServerInfo();
  const authStorageKey = `${appName.toLowerCase().replace(/\s+/g, '_')}_auth`;

  const style = { ...defaultTabBarStyle, ...tabBarStyleProp };

  const [authState, setAuthState] = useState<{ token: string | null; user: AuthUser | null }>({
    token: null,
    user: null
  });

  const basePath = getBasePath();
  const docsAvailable = useDocsAvailable(basePath);
  const dataWorkflowAvailable = useDataWorkflowAvailable(basePath);

  // Build tab list: extra tabs first, then built-in MCP tabs
  const visibleExtraTabs = extraTabs.filter(t => t.visible !== false);
  const mcpTabs = [
    { id: 'tools', label: 'Tools' },
    { id: 'models', label: 'Models', visible: models.length > 0 },
    { id: 'docs', label: 'Docs', visible: docsAvailable === true },
  ].filter(t => t.visible !== false);
  const allTabs = [...visibleExtraTabs, ...mcpTabs];

  const initialTab = defaultTab || (allTabs[0]?.id ?? 'tools');
  const [currentTab, setCurrentTab] = useState(initialTab);

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

  const handleLogin = (token: string, user: AuthUser) => {
    const newAuthState = { token, user };
    setAuthState(newAuthState);
    localStorage.setItem(authStorageKey, JSON.stringify(newAuthState));
  };

  const handleLogout = useCallback(() => {
    setAuthState({ token: null, user: null });
    localStorage.removeItem(authStorageKey);
  }, [authStorageKey]);

  // Sync auth state to api.ts so makeToolRequest auto-injects token
  useEffect(() => {
    setAuthToken(authState.token);
  }, [authState.token]);

  useEffect(() => {
    setOnAuthError(handleLogout);
    return () => setOnAuthError(null);
  }, [handleLogout]);

  const handleTabChange = (tabId: string) => {
    setCurrentTab(tabId);
    const extraTab = extraTabs.find(t => t.id === tabId);
    if (extraTab?.onSelect) {
      extraTab.onSelect();
    }
  };

  // Derive page metadata from server info, with prop fallbacks
  const resolvedTitle = serverInfo?.name
    ? `${serverInfo.name} Tools`
    : pageTitle;
  const resolvedDescription = serverInfo?.description || pageDescription;

  // Whether we're on a built-in MCP tab
  const isMcpTab = currentTab === 'tools' || currentTab === 'models' || currentTab === 'docs';

  // Find active custom tab content
  const activeExtraTab = visibleExtraTabs.find(t => t.id === currentTab);

  return (
    <TrajectoryProvider>
      <Head>
        <title>{resolvedTitle}</title>
        <meta name="description" content={resolvedDescription} />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link rel="icon" href="/favicon.ico" />
      </Head>
      <div className="h-screen flex flex-col overflow-hidden">
        {/* Dark tab bar */}
        <div className={style.bar}>
          <div className="flex items-center">
            <div className="flex">
              {allTabs.map(tab => (
                <button
                  key={tab.id}
                  onClick={() => handleTabChange(tab.id)}
                  className={`px-6 py-3 text-sm font-medium transition-colors ${currentTab === tab.id ? style.active : style.inactive
                    }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>
            <div className="ml-auto px-4 flex items-center gap-3">
              {serverInfo?.mode && (() => {
                const badgeStyle = getModeBadgeStyle(serverInfo.mode);
                return (
                  <span className={`px-2 py-1 text-xs font-medium ${badgeStyle.bg} ${badgeStyle.text} rounded-full`}>
                    {serverInfo.mode.charAt(0).toUpperCase() + serverInfo.mode.slice(1)}
                  </span>
                );
              })()}
              {dataWorkflowAvailable && (
                <Link
                  href="/data-generator"
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-gray-400 border border-gray-700 rounded-md hover:text-white hover:border-gray-500 transition-colors"
                >
                  <svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4" />
                  </svg>
                  Data Workflow
                </Link>
              )}
              {tabBarRight}
              <span className={style.label}>
                {serverInfo?.name || appName} Analytics
              </span>
            </div>
          </div>
        </div>

        {/* Tab content */}
        {activeExtraTab ? (
          // Custom tab content — owns its own layout
          activeExtraTab.content
        ) : isMcpTab ? (
          // Built-in MCP tabs share gradient layout with Header
          <div className="flex-1 overflow-auto">
            <div className="min-h-screen bg-gradient-to-br from-indigo-50 via-white to-purple-50">
              <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
                <Header
                  user={authState.user}
                  onLogout={handleLogout}
                  onLogin={handleLogin}
                  token={authState.token || ''}
                />

                {currentTab === 'tools' && (
                  <ApiTool
                    token={authState.token || ''}
                    user={authState.user}
                    onLogout={handleLogout}
                    onLogin={handleLogin}
                  />
                )}

                {currentTab === 'models' && <ModelsTab />}

                {currentTab === 'docs' && <DocsViewer basePath={basePath} />}
              </div>
            </div>
          </div>
        ) : null}

        {footer}
      </div>
    </TrajectoryProvider>
  );
}

export type { AuthUser };
