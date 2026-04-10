// MCP Tools tab - sidebar with tool categories + tool form/execution panel.
import { useState, useEffect } from 'react';
import { DataType, dataTypes, AuthUser } from '@/lib/api-config';
import ToolsSidebar from '@mcp-shared/ui/ToolsSidebar';
import EmptyState from '@mcp-shared/ui/EmptyState';
import ToolForm from '@mcp-shared/ToolForm';

export interface ApiToolProps {
  token: string;
  user: AuthUser | null;
  onLogout: () => void;
  onLogin: (token: string, user: AuthUser) => void;
}

export default function ApiTool({
  token,
  user,
  onLogout,
  onLogin,
}: ApiToolProps) {
  const [selectedDataType, setSelectedDataType] = useState<DataType | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set());

  // Listen for API_BASE from parent window via postMessage
  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      if (event.data.type === 'SET_API_BASE') {
        console.log('[Iframe] Received API_BASE via postMessage:', event.data.apiBase);
        (window as any).__API_BASE__ = event.data.apiBase;
      }
    };

    window.addEventListener('message', handleMessage);

    // Also check URL params for api_base
    const urlParams = new URLSearchParams(window.location.search);
    const apiBaseParam = urlParams.get('api_base');
    if (apiBaseParam) {
      console.log('[Iframe] Using API_BASE from URL param:', apiBaseParam);
      (window as any).__API_BASE__ = apiBaseParam;
    }

    return () => window.removeEventListener('message', handleMessage);
  }, []);

  // Use static tools from api-config.ts (no runtime discovery)
  const dataTypesByCategory = dataTypes.reduce((acc, dt) => {
    if (!acc[dt.category]) {
      acc[dt.category] = [];
    }
    acc[dt.category].push(dt);
    return acc;
  }, {} as Record<string, DataType[]>);

  const categories = Object.keys(dataTypesByCategory);

  // Filter categories and tools based on search query
  const filteredCategories = categories.filter(cat => {
    const tools = dataTypesByCategory[cat];
    if (tools.length === 0) return false;
    if (!searchQuery) return true;

    const categoryMatches = cat.toLowerCase().includes(searchQuery.toLowerCase());
    const toolMatches = tools.some(dt =>
      dt.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      dt.description.toLowerCase().includes(searchQuery.toLowerCase())
    );
    return categoryMatches || toolMatches;
  });

  const filteredDataTypes = selectedDataType ?
    [] :
    dataTypes.filter(dt => {
      return dt.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        dt.description.toLowerCase().includes(searchQuery.toLowerCase());
    });

  const handleToggleCategory = (category: string) => {
    const newExpanded = new Set(expandedCategories);
    if (expandedCategories.has(category)) {
      newExpanded.delete(category);
    } else {
      newExpanded.add(category);
    }
    setExpandedCategories(newExpanded);
  };

  const handleSelectTool = (dataType: DataType | null) => {
    if (dataType && selectedDataType && selectedDataType.id === dataType.id) {
      setSelectedDataType(null);
    } else {
      setSelectedDataType(dataType);
    }
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Sidebar - Collapsible Categories with Tools */}
      <ToolsSidebar
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        filteredCount={filteredDataTypes.length}
        categories={filteredCategories}
        dataTypesByCategory={dataTypesByCategory}
        expandedCategories={expandedCategories}
        onToggleCategory={handleToggleCategory}
        selectedDataType={selectedDataType}
        onSelectTool={handleSelectTool}
      />

      {/* Main Content */}
      <div className="lg:col-span-2 flex flex-col min-h-0">
        {!selectedDataType ? (
          <EmptyState
            title="Select a tool to get started"
            description="Choose a tool from the sidebar to configure and execute"
          />
        ) : (
          <div className="space-y-6 flex-1 overflow-y-auto">
            <ToolForm
              dataType={selectedDataType}
              token={token}
              onLogin={onLogin}
              onLogout={onLogout}
            />
          </div>
        )}
      </div>
    </div>
  );
}
