interface CsvInputProps {
  value: string;
  onChange: (value: string) => void;
  paramName: string;
  required?: boolean;
  mode: 'upload' | 'paste';
  onModeChange: (mode: 'upload' | 'paste') => void;
}

export default function CsvInput({
  value,
  onChange,
  paramName,
  required,
  mode,
  onModeChange,
}: CsvInputProps) {
  return (
    <div className="space-y-3">
      {/* Toggle between Upload and Paste */}
      <div className="flex gap-2 border-b border-gray-200">
        <button
          type="button"
          onClick={() => onModeChange('upload')}
          className={`px-4 py-2 text-sm font-medium transition-colors ${
            mode === 'upload'
              ? 'text-indigo-700 border-b-2 border-indigo-700'
              : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          Upload File
        </button>
        <button
          type="button"
          onClick={() => onModeChange('paste')}
          className={`px-4 py-2 text-sm font-medium transition-colors ${
            mode === 'paste'
              ? 'text-indigo-700 border-b-2 border-indigo-700'
              : 'text-gray-500 hover:text-gray-700'
          }`}
        >
          Paste Text
        </button>
      </div>

      {/* Upload Mode */}
      {mode === 'upload' && (
        <div className="space-y-2">
          <input
            type="file"
            accept=".csv,text/csv"
            onChange={async (e) => {
              const file = e.target.files?.[0];
              if (file) {
                const text = await file.text();
                onChange(text);
              }
            }}
            className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100"
          />
          <p className="text-xs text-gray-500">
            Upload a CSV file from your computer
          </p>
        </div>
      )}

      {/* Paste Mode */}
      {mode === 'paste' && (
        <div className="space-y-2">
          <textarea
            data-param-name={paramName}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder="Paste your CSV content here...&#10;Example:&#10;name,email,age&#10;John Doe,john@example.com,30&#10;Jane Smith,jane@example.com,25"
            className="w-full rounded-lg border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600 text-sm font-mono"
            rows={8}
            required={required}
          />
          <p className="text-xs text-gray-500">
            Paste CSV text directly (including headers)
          </p>
        </div>
      )}

      {/* Status indicator */}
      {value && (
        <div className="flex items-center justify-between p-2 bg-green-50 border border-green-200 rounded text-xs text-green-700">
          <span>
            CSV loaded ({value.split('\n').length} lines, {value.length} characters)
          </span>
          <button
            type="button"
            onClick={() => onChange('')}
            className="text-red-600 hover:text-red-800 font-medium"
          >
            Clear
          </button>
        </div>
      )}
    </div>
  );
}
