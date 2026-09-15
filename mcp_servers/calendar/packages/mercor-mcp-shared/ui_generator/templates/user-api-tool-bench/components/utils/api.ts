// API utility functions for MCP UI
import axios, { AxiosError, AxiosRequestConfig, AxiosResponse } from 'axios';

// Extract error message from axios error, preferring server's detailed error
export const getErrorMessage = (err: unknown): string => {
  if (axios.isAxiosError(err)) {
    const axiosErr = err as AxiosError<{ detail?: string; message?: string }>;
    // Prefer server's detailed error message
    const serverDetail = axiosErr.response?.data?.detail;
    if (serverDetail) {
      return serverDetail;
    }
    const serverMessage = axiosErr.response?.data?.message;
    if (serverMessage) {
      return serverMessage;
    }
    // Fall back to HTTP status text
    if (axiosErr.response?.statusText) {
      return `${axiosErr.response.status}: ${axiosErr.response.statusText}`;
    }
  }
  // Fall back to generic error message
  return err instanceof Error ? err.message : 'Request failed';
};

// Get base path for static assets (handles Next.js basePath)
export const getBasePath = () => {
  if (typeof window !== 'undefined') {
    // Check if we're running under a basePath (e.g., /ui/server-name)
    const path = window.location.pathname;
    const match = path.match(/^(\/ui\/[^/]+)/);
    if (match) {
      return match[1];
    }
  }
  return '';
};

// Function to get API base URL dynamically (checks window each time)
export const getApiBase = () => {
  if (typeof window !== 'undefined') {
    // Check meta tag first (set during page load)
    const metaTag = document.querySelector('meta[name="api-base"]');
    if (metaTag) {
      const apiBase = metaTag.getAttribute('content');
      if (apiBase) {
        console.log('Using API_BASE from meta tag:', apiBase);
        return apiBase;
      }
    }

    // Check if parent window set __API_BASE__
    if ((window as any).__API_BASE__) {
      console.log('Using injected API_BASE:', (window as any).__API_BASE__);
      return (window as any).__API_BASE__;
    }

    // Check if we're in an iframe and can access parent
    try {
      if (window.parent && window.parent !== window) {
        // Try to read from parent
        if ((window.parent as any).__API_BASE__) {
          console.log('Using API_BASE from parent window:', (window.parent as any).__API_BASE__);
          return (window.parent as any).__API_BASE__;
        }
        if ((window.parent as any).__SERVICE_ID__) {
          const serviceId = (window.parent as any).__SERVICE_ID__;
          const apiBase = `/api/services/${serviceId}/bridge`;
          console.log('Constructed API_BASE from parent service ID:', apiBase);
          return apiBase;
        }
      }
    } catch (e) {
      // Cross-origin, can't access parent
      console.warn('Cannot access parent window:', e);
    }
  }

  const fallback = process.env.NEXT_PUBLIC_API_BASE || '/api/bridge';
  console.log('Using fallback API_BASE:', fallback);
  return fallback;
};

// Helper function to copy text to clipboard with fallback for Chrome
export const copyToClipboard = async (text: string): Promise<boolean> => {
  try {
    // Try modern clipboard API first
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) {
    // Fall through to fallback method
    console.warn('Clipboard API failed, trying fallback:', err);
  }

  // Fallback method for browsers that don't support clipboard API
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.left = '-999999px';
  textarea.style.top = '-999999px';
  document.body.appendChild(textarea);

  try {
    textarea.focus();
    textarea.select();

    const successful = document.execCommand('copy');
    return successful;
  } catch (err) {
    console.error('Failed to copy:', err);
    return false;
  } finally {
    // Always remove textarea from DOM, even if an exception occurs
    document.body.removeChild(textarea);
  }
};

// Options for makeToolRequest
export interface ToolRequestOptions {
  /** Tool endpoint path (e.g., /tools/my_tool) - will be prefixed with API base */
  path: string;
  method?: string;
  data?: Record<string, any>;
  params?: Record<string, any>;
  token?: string | null;
  signal?: AbortSignal;
}

// Make an authenticated request to an MCP tool endpoint
export const makeToolRequest = async <T = any>(
  options: ToolRequestOptions
): Promise<AxiosResponse<T>> => {
  const { path, method = 'POST', data, params, token, signal } = options;
  const apiBase = getApiBase();

  const config: AxiosRequestConfig = {
    method,
    url: `${apiBase}${path}`,
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    },
    signal,
  };

  if (token) {
    config.headers = {
      ...config.headers,
      Authorization: `Bearer ${token}`,
    };
  }

  if (params && Object.keys(params).length > 0) {
    config.params = params;
  }

  if (data && Object.keys(data).length > 0) {
    config.data = data;
  } else if (method === 'POST' || method === 'PUT' || method === 'PATCH') {
    config.data = {};
  }

  try {
    return await axios(config);
  } catch (err) {
    // Re-throw with server's detailed error message for better UX
    const message = getErrorMessage(err);
    const error = new Error(message);
    // Preserve the original error for isCancel checks
    (error as any).originalError = err;
    throw error;
  }
};

// Check if an error is from a cancelled request (handles wrapped errors)
export const isRequestCanceled = (err: unknown): boolean => {
  if (axios.isCancel(err)) {
    return true;
  }
  // Check if it's a wrapped error from makeToolRequest
  const originalError = (err as any)?.originalError;
  if (originalError && axios.isCancel(originalError)) {
    return true;
  }
  return false;
};
