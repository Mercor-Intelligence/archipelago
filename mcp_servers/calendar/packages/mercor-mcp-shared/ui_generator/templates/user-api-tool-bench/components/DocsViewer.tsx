// Documentation viewer component for end_user_documentation folder
// Note: github-markdown-css is imported in _app.tsx
import { useState, useEffect, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import MermaidDiagram from '@mcp-shared/MermaidDiagram';

// Documentation manifest types
export interface DocItem {
  type: 'file' | 'folder';
  id: string;
  title: string;
  path?: string;        // For files
  indexPath?: string;   // For folders
  children?: DocItem[]; // For folders
}

export interface DocsManifest {
  items: DocItem[];
}

interface DocsViewerProps {
  basePath: string;
}

export default function DocsViewer({ basePath }: DocsViewerProps) {
  const [manifest, setManifest] = useState<DocsManifest | null>(null);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [activeSidebarItem, setActiveSidebarItem] = useState<string | null>(null);
  const [contents, setContents] = useState<Record<string, string>>({});
  const [currentContent, setCurrentContent] = useState<string | null>(null);
  const [currentPath, setCurrentPath] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Track current request to handle race conditions
  const currentRequestRef = useRef<string | null>(null);

  // Helper to load doc content by path
  const loadContent = async (path: string) => {
    // Track this request to detect stale responses
    currentRequestRef.current = path;

    // Use full path as cache key to handle basePath changes
    const cacheKey = `${basePath}:${path}`;

    if (contents[cacheKey]) {
      // Set both path and content together to avoid race condition
      setCurrentPath(path);
      setCurrentContent(contents[cacheKey]);
      setError(null);
      return;
    }

    // Clear content while loading to prevent showing old content with new path
    setCurrentContent(null);
    setCurrentPath(path);

    try {
      const res = await fetch(`${basePath}/end_user_documentation/${path}`);

      // Check if this request is still current (handles race condition)
      if (currentRequestRef.current !== path) {
        return; // Stale request, ignore
      }

      if (res.ok) {
        const content = await res.text();
        setContents(prev => ({ ...prev, [cacheKey]: content }));
        setCurrentContent(content);
        setError(null);
      } else {
        setError(`Failed to load content (${res.status})`);
        setCurrentContent(null);
      }
    } catch (e) {
      // Check if this request is still current
      if (currentRequestRef.current !== path) {
        return;
      }
      console.error('Failed to load doc content:', e);
      setError('Failed to load content. Please try again.');
      setCurrentContent(null);
    }
  };

  // Get current doc item from manifest
  const getCurrentItem = (): DocItem | null => {
    if (!manifest || !activeTab) return null;
    return manifest.items.find(item => item.id === activeTab) || null;
  };

  // Handle clicking a tab
  const handleTabClick = (item: DocItem) => {
    setActiveTab(item.id);
    setActiveSidebarItem(null);
    const path = item.type === 'file' ? item.path : item.indexPath;
    if (path) {
      loadContent(path);
    }
  };

  // Handle clicking a sidebar item
  const handleSidebarClick = (item: DocItem) => {
    setActiveSidebarItem(item.id);
    const path = item.type === 'file' ? item.path : item.indexPath;
    if (path) {
      loadContent(path);
    }
  };

  // Load manifest on mount
  useEffect(() => {
    fetch(`${basePath}/end_user_documentation/manifest.json`)
      .then(res => {
        if (res.ok) {
          return res.json().then((data: DocsManifest) => {
            setManifest(data);
            // Auto-select first tab
            if (data.items.length > 0) {
              const first = data.items[0];
              setActiveTab(first.id);
              const path = first.type === 'file' ? first.path : first.indexPath;
              if (path) {
                loadContent(path);
              }
            }
          });
        }
      })
      .catch(e => console.error('Failed to load docs manifest:', e))
      .finally(() => setLoading(false));
  }, [basePath]);

  // Get the directory of the current path for resolving relative image paths
  const getCurrentDir = (): string => {
    if (!currentPath) return '';
    const parts = currentPath.split('/');
    parts.pop(); // Remove filename
    return parts.length > 0 ? parts.join('/') + '/' : '';
  };

  if (loading) {
    return (
      <div className="text-center py-12">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-600"></div>
        <p className="mt-4 text-sm text-gray-600">Loading documentation...</p>
      </div>
    );
  }

  if (!manifest || manifest.items.length === 0) {
    return null;
  }

  const currentItem = getCurrentItem();

  return (
    <div className="space-y-6">
      {/* Sub-tabs for doc sections */}
      {manifest.items.length > 1 && (
        <div className="border-b border-gray-200">
          <nav className="-mb-px flex space-x-6">
            {manifest.items.map(item => (
              <button
                key={item.id}
                onClick={() => handleTabClick(item)}
                className={`py-2 px-1 border-b-2 font-medium text-sm transition-colors ${
                  activeTab === item.id
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                }`}
              >
                {item.title}
              </button>
            ))}
          </nav>
        </div>
      )}

      <div className="flex gap-6">
        {/* Sidebar for folder items */}
        {currentItem?.type === 'folder' && currentItem.children && currentItem.children.length > 0 && (
          <aside className="w-56 flex-shrink-0">
            <nav className="space-y-1">
              {currentItem.children.map(child => (
                <button
                  key={child.id}
                  onClick={() => handleSidebarClick(child)}
                  className={`block w-full text-left px-3 py-2 rounded text-sm transition-colors ${
                    activeSidebarItem === child.id
                      ? 'bg-indigo-50 text-indigo-700 font-medium'
                      : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'
                  }`}
                >
                  {child.title}
                </button>
              ))}
            </nav>
          </aside>
        )}

        {/* Main content area */}
        <div className="flex-1 min-w-0">
          <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-8">
            {error ? (
              <div className="text-center py-12">
                <p className="text-red-600">{error}</p>
                <button
                  onClick={() => currentPath && loadContent(currentPath)}
                  className="mt-4 text-sm text-indigo-600 hover:text-indigo-800"
                >
                  Try again
                </button>
              </div>
            ) : currentContent !== null ? (
              <article className="markdown-body">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    img({ src, alt, ...props }: any) {
                      // Transform relative paths to work with public/end_user_documentation/
                      // Skip absolute URLs, root-relative paths, and data URIs
                      if (src && !src.startsWith('http') && !src.startsWith('/') && !src.startsWith('data:')) {
                        // Normalize paths: remove ./ prefix if present
                        const normalizedSrc = src.startsWith('./') ? src.slice(2) : src;
                        src = `${basePath}/end_user_documentation/${getCurrentDir()}${normalizedSrc}`;
                      }
                      return <img src={src} alt={alt} {...props} />;
                    },
                    code({ node, children, ...props }: any) {
                      const className = node?.properties?.className?.join?.(' ') || node?.properties?.className || '';
                      const match = /language-(\w+)/.exec(className);
                      const language = match ? match[1] : '';
                      const isInline = !node?.properties?.className;

                      if (!isInline && language === 'mermaid') {
                        return <MermaidDiagram chart={String(children).replace(/\n$/, '')} />;
                      }

                      return (
                        <code className={className} {...props}>
                          {children}
                        </code>
                      );
                    }
                  }}
                >
                  {currentContent}
                </ReactMarkdown>
              </article>
            ) : (
              <div className="text-center py-12">
                <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-indigo-600"></div>
                <p className="mt-4 text-sm text-gray-600">Loading...</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// Hook to check if docs are available (for conditional tab rendering)
export function useDocsAvailable(basePath: string): boolean | null {
  const [available, setAvailable] = useState<boolean | null>(null);

  useEffect(() => {
    fetch(`${basePath}/end_user_documentation/manifest.json`)
      .then(res => {
        if (!res.ok) {
          setAvailable(false);
          return;
        }
        return res.json().then((data: DocsManifest) => {
          // Only show docs tab if manifest has items
          setAvailable(data.items && data.items.length > 0);
        });
      })
      .catch(() => setAvailable(false));
  }, [basePath]);

  return available;
}
