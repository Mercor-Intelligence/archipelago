// File Browser component for MCP filesystem tools
import { useState, useEffect, useCallback } from 'react';
import dynamic from 'next/dynamic';
import { makeToolRequest } from './utils/api';
import { canPreviewFile } from './FilePreview';

// Dynamically import FilePreview - only loaded when user clicks Preview
const FilePreview = dynamic(() => import('./FilePreview'), {
  ssr: false,
  loading: () => null,
});

interface FsRoot {
  alias: string;
  path: string;
  readonly: boolean;
}

interface FileInfo {
  name: string;
  path: string;
  is_directory: boolean;
  size_bytes: number | null;
  modified_at: string | null;
  mime_type: string | null;
}

interface FileBrowserProps {
  token?: string;
}

export default function FileBrowser({ token }: FileBrowserProps) {
  const [roots, setRoots] = useState<FsRoot[]>([]);
  const [selectedRoot, setSelectedRoot] = useState<string | null>(null);
  const [currentPath, setCurrentPath] = useState('');
  const [parentPath, setParentPath] = useState<string | null>(null);
  const [items, setItems] = useState<FileInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [downloadingFile, setDownloadingFile] = useState<string | null>(null);
  const [previewingFile, setPreviewingFile] = useState<string | null>(null);
  const [previewData, setPreviewData] = useState<{
    fileName: string;
    content: string;
    mimeType: string | null;
  } | null>(null);

  // Call a tool via the REST API
  const callTool = useCallback(async (toolName: string, params: Record<string, unknown>) => {
    const response = await makeToolRequest({
      toolName,
      data: params,
      token,
    });
    return response.data;
  }, [token]);

  // Load available roots on mount (only once)
  useEffect(() => {
    let cancelled = false;
    const loadRoots = async () => {
      try {
        setError(null);
        const data = await callTool('list_fs_roots', {});
        if (cancelled) return;
        const rootsList = data.roots || [];
        setRoots(rootsList);
        if (rootsList.length > 0) {
          setSelectedRoot((prev) => prev || rootsList[0].alias);
        }
      } catch (err: unknown) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : 'Failed to load roots';
        setError(message);
      }
    };
    loadRoots();
    return () => { cancelled = true; };
  }, [callTool]);

  // Load folder contents when root or path changes
  useEffect(() => {
    if (!selectedRoot) return;

    let cancelled = false;
    const loadContents = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await callTool('list_folder', {
          root: selectedRoot,
          path: currentPath,
        });
        if (cancelled) return;
        setItems(data.items || []);
        setParentPath(data.parent_path);
      } catch (err: unknown) {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : 'Failed to load folder';
        setError(message);
        setItems([]);
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };
    loadContents();
    return () => { cancelled = true; };
  }, [selectedRoot, currentPath, callTool]);

  // Handle file download
  const handleDownload = async (item: FileInfo) => {
    if (!selectedRoot) return;

    setDownloadingFile(item.path);
    try {
      const data = await callTool('download_file', {
        root: selectedRoot,
        path: item.path,
      });

      // Decode base64 and trigger download
      const binaryString = atob(data.content_base64);
      const bytes = new Uint8Array(binaryString.length);
      for (let i = 0; i < binaryString.length; i++) {
        bytes[i] = binaryString.charCodeAt(i);
      }
      const blob = new Blob([bytes], { type: data.mime_type || 'application/octet-stream' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = data.file_name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Download failed';
      setError(message);
    } finally {
      setDownloadingFile(null);
    }
  };

  // Handle file preview
  const handlePreview = async (item: FileInfo) => {
    if (!selectedRoot) return;

    setPreviewingFile(item.path);
    try {
      const data = await callTool('download_file', {
        root: selectedRoot,
        path: item.path,
      });

      setPreviewData({
        fileName: data.file_name,
        content: data.content_base64,
        mimeType: data.mime_type,
      });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Preview failed';
      setError(message);
    } finally {
      setPreviewingFile(null);
    }
  };

  // Close preview
  const closePreview = () => {
    setPreviewData(null);
  };

  // Format file size
  const formatSize = (bytes: number | null) => {
    if (bytes === null) return '-';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  };

  // Format date
  const formatDate = (isoDate: string | null) => {
    if (!isoDate) return '-';
    try {
      return new Date(isoDate).toLocaleString();
    } catch {
      return isoDate;
    }
  };

  // Build breadcrumb parts
  const breadcrumbs = currentPath ? currentPath.split('/').filter(Boolean) : [];

  return (
    <div className="bg-white rounded-lg border border-gray-200 shadow-sm">
      {/* Header */}
      <div className="border-b border-gray-200 p-4">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-900">File Browser</h2>
          {roots.length > 0 && (
            <select
              value={selectedRoot || ''}
              onChange={(e) => {
                setSelectedRoot(e.target.value);
                setCurrentPath('');
              }}
              className="rounded-lg border border-gray-300 px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-transparent"
            >
              {roots.map((root) => (
                <option key={root.alias} value={root.alias}>
                  {root.alias} {root.readonly ? '(read-only)' : ''}
                </option>
              ))}
            </select>
          )}
        </div>

        {/* Breadcrumbs */}
        <nav className="flex items-center gap-1 text-sm text-gray-600 overflow-x-auto">
          <button
            onClick={() => setCurrentPath('')}
            className="hover:text-blue-600 hover:underline font-medium shrink-0"
          >
            Root
          </button>
          {breadcrumbs.map((part, i) => (
            <span key={i} className="flex items-center gap-1 shrink-0">
              <span className="text-gray-400">/</span>
              <button
                onClick={() => setCurrentPath(breadcrumbs.slice(0, i + 1).join('/'))}
                className="hover:text-blue-600 hover:underline"
              >
                {part}
              </button>
            </span>
          ))}
        </nav>
      </div>

      {/* Error message */}
      {error && (
        <div className="m-4 p-3 bg-red-50 border border-red-200 text-red-700 rounded-lg text-sm">
          {error}
        </div>
      )}

      {/* Empty state for no roots */}
      {roots.length === 0 && !loading && !error && (
        <div className="p-8 text-center text-gray-500">
          <svg className="w-12 h-12 mx-auto mb-4 text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
          </svg>
          <p>No filesystem roots configured.</p>
          <p className="text-sm mt-1">Set the STATE_LOCATION environment variable to enable file browsing.</p>
        </div>
      )}

      {/* File list */}
      {roots.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-gray-600">Name</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600 w-24">Size</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600 w-44">Modified</th>
                <th className="text-right px-4 py-3 font-medium text-gray-600 w-28">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {loading ? (
                <tr>
                  <td colSpan={4} className="px-4 py-8 text-center text-gray-500">
                    <div className="inline-flex items-center gap-2">
                      <svg className="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                      </svg>
                      Loading...
                    </div>
                  </td>
                </tr>
              ) : (
                <>
                  {/* Parent directory link */}
                  {parentPath !== null && (
                    <tr
                      className="hover:bg-gray-50 cursor-pointer"
                      onClick={() => setCurrentPath(parentPath)}
                    >
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2 text-gray-500">
                          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                            <path fillRule="evenodd" d="M12.707 5.293a1 1 0 010 1.414L9.414 10l3.293 3.293a1 1 0 01-1.414 1.414l-4-4a1 1 0 010-1.414l4-4a1 1 0 011.414 0z" clipRule="evenodd" />
                          </svg>
                          <span>..</span>
                          <span className="text-gray-400 text-xs">(Parent folder)</span>
                        </div>
                      </td>
                      <td></td>
                      <td></td>
                      <td></td>
                    </tr>
                  )}

                  {/* Files and folders */}
                  {items.map((item) => (
                    <tr
                      key={item.path}
                      className={`hover:bg-gray-50 ${item.is_directory ? 'cursor-pointer' : ''}`}
                      onClick={() => item.is_directory && setCurrentPath(item.path)}
                    >
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          {item.is_directory ? (
                            <svg className="w-5 h-5 text-yellow-500 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                              <path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" />
                            </svg>
                          ) : (
                            <svg className="w-5 h-5 text-gray-400 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                              <path fillRule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z" clipRule="evenodd" />
                            </svg>
                          )}
                          <span className={item.is_directory ? 'text-blue-600 font-medium' : 'text-gray-900'}>
                            {item.name}
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-right text-gray-500">
                        {formatSize(item.size_bytes)}
                      </td>
                      <td className="px-4 py-3 text-right text-gray-500">
                        {formatDate(item.modified_at)}
                      </td>
                      <td className="px-4 py-3 text-right">
                        {!item.is_directory && (
                          <div className="flex items-center justify-end gap-2">
                            {canPreviewFile(item.mime_type, item.name) && (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handlePreview(item);
                                }}
                                disabled={previewingFile === item.path}
                                className="px-3 py-1.5 text-xs font-medium bg-gray-100 text-gray-700 rounded-md hover:bg-gray-200 disabled:bg-gray-50 disabled:text-gray-400 disabled:cursor-not-allowed transition-colors"
                              >
                                {previewingFile === item.path ? 'Loading...' : 'Preview'}
                              </button>
                            )}
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                handleDownload(item);
                              }}
                              disabled={downloadingFile === item.path}
                              className="px-3 py-1.5 text-xs font-medium bg-blue-600 text-white rounded-md hover:bg-blue-700 disabled:bg-blue-300 disabled:cursor-not-allowed transition-colors"
                            >
                              {downloadingFile === item.path ? 'Downloading...' : 'Download'}
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}

                  {/* Empty folder */}
                  {items.length === 0 && !loading && (
                    <tr>
                      <td colSpan={4} className="px-4 py-8 text-center text-gray-500">
                        This folder is empty
                      </td>
                    </tr>
                  )}
                </>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* File Preview Modal */}
      {previewData && (
        <FilePreview
          fileName={previewData.fileName}
          content={previewData.content}
          mimeType={previewData.mimeType}
          onClose={closePreview}
        />
      )}
    </div>
  );
}

// Hook to check if filesystem roots are available
export function useFilesAvailable(): number {
  const [count, setCount] = useState(0);

  useEffect(() => {
    const checkFilesAvailable = async () => {
      try {
        const response = await makeToolRequest({
          toolName: 'list_fs_roots',
          data: {},
        });
        setCount(response.data?.roots?.length || 0);
      } catch {
        setCount(0);
      }
    };
    checkFilesAvailable();
  }, []);

  return count;
}
