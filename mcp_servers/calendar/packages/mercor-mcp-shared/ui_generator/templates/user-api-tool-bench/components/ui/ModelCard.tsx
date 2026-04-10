import { Model } from '@/lib/api-config';

interface ModelCardProps {
  model: Model;
  onClick: () => void;
}

export default function ModelCard({ model, onClick }: ModelCardProps) {
  const fieldCount = Object.keys(model.fields).length;
  const requiredCount = Object.values(model.fields).filter((f) => f.required).length;

  return (
    <div
      onClick={onClick}
      className="bg-white rounded-lg shadow-sm border border-gray-200 p-5 hover:shadow-md hover:border-indigo-300 transition-all cursor-pointer"
    >
      <div className="flex items-start justify-between mb-2">
        <h3 className="text-lg font-semibold text-gray-900">{model.name}</h3>
        {model.is_enum && (
          <span className="px-2 py-1 text-xs bg-purple-100 text-purple-700 rounded-full">
            Enum
          </span>
        )}
      </div>

      {model.docstring && (
        <p className="text-gray-600 text-sm mb-3 line-clamp-2">{model.docstring}</p>
      )}

      <div className="flex items-center gap-4 text-sm text-gray-500">
        <span><strong>{fieldCount}</strong> {fieldCount === 1 ? 'field' : 'fields'}</span>
        {!model.is_enum && <span><strong>{requiredCount}</strong> required</span>}
      </div>
    </div>
  );
}
