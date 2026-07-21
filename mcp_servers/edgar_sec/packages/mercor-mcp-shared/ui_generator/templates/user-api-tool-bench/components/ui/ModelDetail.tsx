import { Model } from '@/lib/api-config';
import ModelFieldRow from './ModelFieldRow';

interface ModelDetailProps {
  model: Model;
}

export default function ModelDetail({ model }: ModelDetailProps) {
  const fieldCount = Object.keys(model.fields).length;
  const requiredCount = Object.values(model.fields).filter((f) => f.required).length;

  return (
    <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-6">
      {/* Header */}
      <div className="mb-6 pb-6 border-b border-gray-200">
        <div className="flex items-center gap-3 mb-3">
          <h2 className="text-2xl font-bold text-gray-900">{model.name}</h2>
          {model.is_enum && (
            <span className="px-3 py-1 text-sm bg-purple-100 text-purple-700 rounded-full">
              Enum
            </span>
          )}
        </div>

        {model.docstring && (
          <p className="text-gray-600 whitespace-pre-wrap text-sm">{model.docstring}</p>
        )}

        <div className="flex items-center gap-4 mt-4 text-sm text-gray-500">
          <span><strong>{fieldCount}</strong> {fieldCount === 1 ? 'field' : 'fields'}</span>
          {!model.is_enum && <span><strong>{requiredCount}</strong> required</span>}
        </div>

        {model.bases.length > 0 && (
          <div className="mt-3">
            <span className="text-sm text-gray-500">
              Extends: <code className="text-indigo-600">{model.bases.join(', ')}</code>
            </span>
          </div>
        )}
      </div>

      {/* Fields */}
      <div>
        <h3 className="text-lg font-semibold mb-4 text-gray-900">
          {model.is_enum ? 'Values' : 'Fields'}
        </h3>
        <div className="space-y-3">
          {Object.entries(model.fields).map(([fieldName, field]) => (
            <ModelFieldRow
              key={fieldName}
              fieldName={fieldName}
              field={field}
              isEnum={model.is_enum}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
