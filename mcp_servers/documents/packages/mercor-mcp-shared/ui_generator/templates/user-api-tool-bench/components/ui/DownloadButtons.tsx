import { getExportableArrays, downloadJson, downloadCsv } from '../downloadUtils';
import CollapsibleJson from './CollapsibleJson';

interface DownloadButtonsProps {
  response: any;
  showRawJsonToggle?: boolean;
}

export default function DownloadButtons({ response, showRawJsonToggle = false }: DownloadButtonsProps) {
  const hasExportableArrays = getExportableArrays(response).length > 0;

  return (
    <div className="flex items-center gap-3 flex-wrap">
      {showRawJsonToggle && (
        <details className="group">
          <summary className="cursor-pointer text-xs text-gray-600 hover:text-gray-900 font-medium list-none">
            <span className="inline-block group-open:rotate-90 transition-transform mr-1">▶</span>
            View Raw JSON
          </summary>
          <div className="mt-2">
            <CollapsibleJson data={response} />
          </div>
        </details>
      )}
      <button
        onClick={() => downloadJson(response)}
        className="px-2 py-1 text-xs bg-green-100 text-green-700 rounded hover:bg-green-200 transition-colors font-medium"
      >
        Download JSON
      </button>
      {hasExportableArrays && (
        <button
          onClick={() => downloadCsv(response)}
          className="px-2 py-1 text-xs bg-blue-100 text-blue-700 rounded hover:bg-blue-200 transition-colors font-medium"
        >
          Download CSV
        </button>
      )}
    </div>
  );
}
