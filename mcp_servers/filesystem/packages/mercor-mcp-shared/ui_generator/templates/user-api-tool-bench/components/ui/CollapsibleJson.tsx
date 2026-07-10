// Collapsible JSON display component
import { useState } from 'react';
import { copyToClipboard } from '../utils/api';

interface CollapsibleJsonProps {
  data: any;
  maxPreviewLength?: number;
}

export default function CollapsibleJson({ data, maxPreviewLength = 100 }: CollapsibleJsonProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const jsonString = JSON.stringify(data, null, 2);
  const preview = JSON.stringify(data).substring(0, maxPreviewLength);
  const needsCollapse = jsonString.length > maxPreviewLength;

  const handleCopy = async () => {
    const success = await copyToClipboard(jsonString);
    if (success) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  if (!needsCollapse) {
    return (
      <div className="space-y-2">
        <button
          onClick={handleCopy}
          className="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200 transition-colors font-medium"
        >
          {copied ? '✓ Copied!' : 'Copy JSON'}
        </button>
        <pre className="font-mono text-xs text-gray-700 whitespace-pre-wrap bg-gray-50 p-3 rounded border border-gray-200">{jsonString}</pre>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {!isExpanded ? (
        <div className="flex items-start gap-2">
          <button
            onClick={() => setIsExpanded(true)}
            className="flex-shrink-0 px-2 py-1 text-xs bg-indigo-100 text-indigo-700 rounded hover:bg-indigo-200 transition-colors font-medium"
          >
            Expand JSON
          </button>
          <code className="flex-1 font-mono text-xs text-gray-500 truncate">
            {preview}...
          </code>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex gap-2">
            <button
              onClick={() => setIsExpanded(false)}
              className="px-2 py-1 text-xs bg-indigo-100 text-indigo-700 rounded hover:bg-indigo-200 transition-colors font-medium"
            >
              Collapse JSON
            </button>
            <button
              onClick={handleCopy}
              className="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded hover:bg-gray-200 transition-colors font-medium"
            >
              {copied ? '✓ Copied!' : 'Copy JSON'}
            </button>
          </div>
          <pre className="font-mono text-xs text-gray-700 whitespace-pre-wrap bg-gray-50 p-3 rounded border border-gray-200 max-h-96 overflow-auto">
            {jsonString}
          </pre>
        </div>
      )}
    </div>
  );
}
