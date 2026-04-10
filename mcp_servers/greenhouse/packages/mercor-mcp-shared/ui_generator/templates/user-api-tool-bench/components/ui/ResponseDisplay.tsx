import { renderValue } from './ValueRenderer';
import DownloadButtons from './DownloadButtons';
import CollapsibleJson from './CollapsibleJson';

interface ResponseDisplayProps {
  response: any;
}

// Check if a key/value pair represents base64 image data
function isBase64ImageData(key: string, value: any, responseData: any): boolean {
  if (typeof value !== 'string') return false;

  // Check for common image data field names
  const imageFieldNames = ['image_data_base64', 'image_data', 'image_base64', 'base64_image'];
  const isImageField = imageFieldNames.some(name => key.toLowerCase() === name.toLowerCase());

  // Check if response indicates image content type
  const isImageContentType =
    responseData.content_type?.startsWith('image/') ||
    responseData.format === 'png' ||
    responseData.format === 'jpg' ||
    responseData.format === 'jpeg';

  return isImageField && isImageContentType;
}

// Detect image format from content_type or format field
function getImageFormat(responseData: any): string {
  if (responseData.content_type) {
    // MIME subtypes can contain letters, digits, and . + - characters (e.g., svg+xml)
    const match = responseData.content_type.match(/image\/([a-zA-Z0-9.+-]+)/);
    if (match) return match[1];
  }
  if (responseData.format) {
    return responseData.format;
  }
  return 'png'; // default
}

export default function ResponseDisplay({ response }: ResponseDisplayProps) {
  if (!response) return null;

  const isObjectResponse = response.data && typeof response.data === 'object' && !Array.isArray(response.data);

  return (
    <div className="mt-6">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-900 uppercase tracking-wide">Response</h3>
        {response.metadata?.duration && (
          <span className="text-xs text-gray-500">
            {response.metadata.duration}ms
          </span>
        )}
      </div>

      {/* Smart rendering: show data nicely if it's an object */}
      {isObjectResponse ? (
        <div className="space-y-4">
          <div className="bg-gradient-to-br from-indigo-50 to-purple-50 rounded-lg p-4 border border-indigo-200">
            {Object.entries(response.data).map(([key, value]) => (
              <div key={key} className="mb-3 last:mb-0">
                <div className="text-xs font-semibold text-indigo-900 uppercase tracking-wide mb-1">
                  {key.replace(/_/g, ' ')}
                </div>
                <div className="text-sm text-gray-800">
                  {isBase64ImageData(key, value, response.data) ? (
                    <Base64ImageDisplay
                      value={value as string}
                      format={getImageFormat(response.data)}
                      viewId={response.data.view_id}
                    />
                  ) : key === 'csv_content' && typeof value === 'string' ? (
                    <CsvContentDisplay value={value} tableName={response.data.table_name} />
                  ) : (
                    renderValue(value, 1, key)
                  )}
                </div>
              </div>
            ))}
          </div>

          {/* Download buttons and raw JSON toggle */}
          <DownloadButtons response={response} showRawJsonToggle />
        </div>
      ) : (
        <div className="space-y-3">
          <DownloadButtons response={response} />
          <CollapsibleJson data={response} />
        </div>
      )}
    </div>
  );
}

// Sub-component for base64 image display (PNG, JPG, etc.)
function Base64ImageDisplay({
  value,
  format,
  viewId,
}: {
  value: string;
  format: string;
  viewId?: string;
}) {
  const handleDownload = () => {
    // Decode base64 to binary
    const byteCharacters = atob(value);
    const byteNumbers = new Array(byteCharacters.length);
    for (let i = 0; i < byteCharacters.length; i++) {
      byteNumbers[i] = byteCharacters.charCodeAt(i);
    }
    const byteArray = new Uint8Array(byteNumbers);
    const blob = new Blob([byteArray], { type: `image/${format}` });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${viewId ? `view_${viewId}` : 'export'}.${format}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-3">
      <div className="border border-gray-200 rounded-lg overflow-hidden bg-white p-2">
        <img
          src={`data:image/${format};base64,${value}`}
          alt="Response visualization"
          className="max-w-full h-auto rounded"
          style={{ maxHeight: '500px' }}
        />
      </div>
      <div className="flex gap-2 items-center">
        <button
          onClick={handleDownload}
          className="px-3 py-1.5 text-xs font-medium text-white bg-indigo-600 rounded hover:bg-indigo-700"
        >
          Download {format.toUpperCase()}
        </button>
        {viewId && (
          <span className="text-xs text-gray-500">
            View ID: {viewId}
          </span>
        )}
      </div>
    </div>
  );
}

// Sub-component for CSV content display
function CsvContentDisplay({ value, tableName }: { value: string; tableName?: string }) {
  const handleDownload = () => {
    const blob = new Blob([value], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${tableName || 'export'}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-2">
      <div className="text-xs text-gray-600">
        {value.substring(0, 200)}...
      </div>
      <button
        onClick={handleDownload}
        className="px-3 py-1 text-xs font-medium text-white bg-indigo-600 rounded hover:bg-indigo-700"
      >
        Download CSV
      </button>
    </div>
  );
}
