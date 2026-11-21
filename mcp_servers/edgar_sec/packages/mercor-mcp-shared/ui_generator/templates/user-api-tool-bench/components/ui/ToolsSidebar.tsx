import { DataType } from '@/lib/api-config';
import SearchBar from './SearchBar';

interface ToolsSidebarProps {
  searchQuery: string;
  onSearchChange: (query: string) => void;
  filteredCount: number;
  categories: string[];
  dataTypesByCategory: Record<string, DataType[]>;
  expandedCategories: Set<string>;
  onToggleCategory: (category: string) => void;
  selectedDataType: DataType | null;
  onSelectTool: (dataType: DataType | null) => void;
}

export default function ToolsSidebar({
  searchQuery,
  onSearchChange,
  filteredCount,
  categories,
  dataTypesByCategory,
  expandedCategories,
  onToggleCategory,
  selectedDataType,
  onSelectTool,
}: ToolsSidebarProps) {
  return (
    <div className="lg:col-span-1 flex flex-col space-y-4">
      <SearchBar
        value={searchQuery}
        onChange={onSearchChange}
        placeholder="Search tools..."
      />

      {searchQuery && (
        <div className="text-xs text-gray-500">
          {filteredCount} {filteredCount === 1 ? 'result' : 'results'} found
        </div>
      )}

      <div className="flex-1 overflow-y-auto pr-2 space-y-2">
        {categories.map(category => {
          const allTools = dataTypesByCategory[category];
          // Filter tools: exclude hidden tools and apply search query
          const tools = allTools.filter(dt => {
            // Skip hidden tools (they're in dataTypes for programmatic access but not shown in sidebar)
            if (dt.hidden) return false;
            if (!searchQuery) return true;
            return dt.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
              dt.description.toLowerCase().includes(searchQuery.toLowerCase());
          });

          // Skip categories with no visible tools
          if (tools.length === 0) return null;

          const isCategoryExpanded = expandedCategories.has(category);

          // Category color scheme - using consistent gray for all categories
          const colors = { bg: 'bg-gray-50', border: 'border-gray-200', text: 'text-gray-800' };

          return (
            <div key={category} className={`border-2 ${colors.border} rounded-lg overflow-hidden`}>
              <button
                onClick={() => onToggleCategory(category)}
                className={`w-full flex items-center justify-between px-4 py-3 ${colors.bg} hover:opacity-90 transition-all`}
              >
                <div className="flex items-center gap-2">
                  <svg className={`w-4 h-4 ${colors.text} transition-transform ${isCategoryExpanded ? 'rotate-90' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                  </svg>
                  <span className={`text-sm font-semibold ${colors.text}`}>{category}</span>
                </div>
                <span className={`text-xs font-medium ${colors.text}`}>{tools.length}</span>
              </button>
              {isCategoryExpanded && (
                <div className="p-2 space-y-1 bg-white">
                  {tools.map((dataType) => {
                    const selectedId = selectedDataType?.id;
                    const isSelected = selectedId === dataType.id;
                    return (
                      <button
                        key={dataType.id}
                        data-testid={`tool-${dataType.id}`}
                        data-tool-id={dataType.id}
                        data-tool-name={dataType.name}
                        onClick={() => {
                          if (selectedId === dataType.id) {
                            onSelectTool(null);
                          } else {
                            onSelectTool(dataType);
                          }
                        }}
                        className={`w-full text-left px-3 py-2 rounded-md transition-all text-sm ${
                          isSelected
                            ? 'bg-indigo-50 text-indigo-900 font-medium border border-indigo-200'
                            : 'text-gray-700 hover:bg-gray-50 border border-transparent'
                        }`}
                      >
                        <div className="font-medium">{dataType.name}</div>
                        <div className="text-xs text-gray-500 mt-0.5">{dataType.description.slice(0, 80)}{dataType.description.length > 80 ? '...' : ''}</div>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
