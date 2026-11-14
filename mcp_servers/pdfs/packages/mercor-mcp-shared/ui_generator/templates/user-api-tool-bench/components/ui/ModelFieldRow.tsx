import { ModelField } from '@/lib/api-config';

interface ModelFieldRowProps {
  fieldName: string;
  field: ModelField;
  isEnum?: boolean;
}

export default function ModelFieldRow({ fieldName, field, isEnum = false }: ModelFieldRowProps) {
  return (
    <div className="border border-gray-200 rounded-lg p-4 hover:border-indigo-300 transition-colors">
      <div className="flex items-start justify-between mb-2">
        <div className="flex items-center gap-2">
          <code className="text-indigo-600 font-semibold">{fieldName}</code>
          {field.required && !isEnum && (
            <span className="px-2 py-0.5 text-xs bg-red-100 text-red-700 rounded">
              Required
            </span>
          )}
        </div>
        <code className="text-sm text-gray-600 bg-gray-100 px-2 py-1 rounded">
          {field.type}
        </code>
      </div>

      {field.description && (
        <p className="text-gray-600 text-sm mb-2">{field.description}</p>
      )}

      {field.default !== null && !isEnum && (
        <div className="text-sm text-gray-500">
          Default: <code className="bg-gray-100 px-2 py-0.5 rounded">
            {typeof field.default === 'object' ? JSON.stringify(field.default) : String(field.default)}
          </code>
        </div>
      )}
    </div>
  );
}
