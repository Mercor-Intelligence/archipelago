import { DataType } from '@/lib/api-config';

interface ToolHeaderProps {
  dataType: DataType;
}

export default function ToolHeader({ dataType }: ToolHeaderProps) {
  return (
    <div className="mb-6">
      <div className="flex items-center gap-3 mb-2">
        <h2 className="text-xl font-semibold text-gray-900">
          {dataType.name}
        </h2>
        <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-indigo-100 text-indigo-800">
          {dataType.category}
        </span>
      </div>
      <p className="text-sm text-gray-600">{dataType.description}</p>
    </div>
  );
}
