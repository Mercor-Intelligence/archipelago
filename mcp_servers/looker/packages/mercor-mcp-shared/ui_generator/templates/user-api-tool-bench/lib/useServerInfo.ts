// Shared module for fetching and caching server_info.
// Singleton: only one network request fires regardless of how many components
// call useServerInfo(). All callers share the cached result.

import { useState, useEffect } from 'react';
import { getApiBase } from '@mcp-shared/utils/api';

export interface ServerInfoResponse {
  name: string;
  version: string;
  description: string;
  mode?: string | null;
  status: string;
  features: {
    authentication: boolean;
    personas?: string[] | null;
    persistence?: string | null;
    api_compatibility?: string | null;
    [key: string]: unknown;
  };
  tool_categories?: Array<{
    name: string;
    tools: Array<{ name: string; actions?: string[] | null }>;
  }> | null;
}

export interface UseServerInfoResult {
  serverInfo: ServerInfoResponse | null;
  loading: boolean;
  error: string | null;
}

// Module-level singleton cache
let cachedServerInfo: ServerInfoResponse | null = null;
let fetchPromise: Promise<ServerInfoResponse | null> | null = null;

async function fetchServerInfo(): Promise<ServerInfoResponse | null> {
  try {
    const apiBase = getApiBase();
    const response = await fetch(`${apiBase}/tools/server_info`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });

    if (response.ok) {
      const data: ServerInfoResponse = await response.json();
      cachedServerInfo = data;
      return data;
    } else {
      console.warn('server_info tool returned non-OK status:', response.status);
      return null;
    }
  } catch (error) {
    console.warn('Failed to fetch server_info:', error);
    return null;
  }
}

export function useServerInfo(): UseServerInfoResult {
  const [serverInfo, setServerInfo] = useState<ServerInfoResponse | null>(cachedServerInfo);
  const [loading, setLoading] = useState(!cachedServerInfo);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (cachedServerInfo) {
      setServerInfo(cachedServerInfo);
      setLoading(false);
      return;
    }

    // Deduplicate: reuse in-flight fetch
    if (!fetchPromise) {
      fetchPromise = fetchServerInfo();
    }

    fetchPromise
      .then((data) => {
        setServerInfo(data);
        setLoading(false);
        fetchPromise = null;
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : 'Failed to fetch server info');
        setLoading(false);
        fetchPromise = null;
      });
  }, []);

  return { serverInfo, loading, error };
}

// Badge color utility for mode values
export function getModeBadgeStyle(mode: string): { bg: string; text: string } {
  switch (mode.toLowerCase()) {
    case 'online':
      return { bg: 'bg-green-100', text: 'text-green-800' };
    case 'offline':
      return { bg: 'bg-yellow-100', text: 'text-yellow-800' };
    case 'hybrid':
      return { bg: 'bg-blue-100', text: 'text-blue-800' };
    default:
      return { bg: 'bg-gray-100', text: 'text-gray-800' };
  }
}
