// Models tab - browse and inspect Pydantic model schemas.
// Self-contained: imports models from api-config and manages its own state.
import { useState } from 'react';
import { models } from '@/lib/api-config';
import SearchBar from '@mcp-shared/ui/SearchBar';
import ModelCard from '@mcp-shared/ui/ModelCard';
import ModelDetail from '@mcp-shared/ui/ModelDetail';

export default function ModelsTab() {
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');

  const filteredModels = models.filter(m =>
    m.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    (m.docstring?.toLowerCase().includes(searchQuery.toLowerCase()) ?? false)
  );

  return (
    <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
      {/* Sidebar - Model List */}
      <div className="lg:col-span-1">
        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 sticky top-4">
          <div className="mb-4">
            <SearchBar
              value={searchQuery}
              onChange={setSearchQuery}
              placeholder="Search models..."
            />
          </div>
          <h2 className="text-sm font-semibold mb-3 text-gray-700 uppercase tracking-wide">
            Models ({filteredModels.length})
          </h2>
          <div className="space-y-1 max-h-[calc(100vh-16rem)] overflow-y-auto">
            {filteredModels.map((model) => (
              <button
                key={model.name}
                onClick={() => setSelectedModel(selectedModel === model.name ? null : model.name)}
                className={`w-full text-left px-3 py-2 rounded-md transition-colors text-sm ${
                  selectedModel === model.name
                    ? 'bg-indigo-50 text-indigo-900 font-medium border border-indigo-200'
                    : 'hover:bg-gray-50 text-gray-700 border border-transparent'
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="truncate">{model.name}</span>
                  {model.is_enum && (
                    <span className="ml-2 px-2 py-0.5 text-xs bg-purple-100 text-purple-700 rounded">
                      Enum
                    </span>
                  )}
                </div>
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Main Content */}
      <div className="lg:col-span-3">
        {selectedModel ? (
          (() => {
            const model = models.find(m => m.name === selectedModel);
            if (!model) return null;
            return <ModelDetail model={model} />;
          })()
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {filteredModels.map((model) => (
              <ModelCard
                key={model.name}
                model={model}
                onClick={() => setSelectedModel(model.name)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
