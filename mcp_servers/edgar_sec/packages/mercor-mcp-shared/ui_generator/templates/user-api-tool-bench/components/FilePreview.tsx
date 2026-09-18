// File Preview component with lazy-loaded syntax highlighting
import { useState, useEffect, useCallback } from 'react';
import dynamic from 'next/dynamic';
import Papa from 'papaparse';

// Dynamically import heavy components - only loaded when preview is opened
// Use CJS build to avoid property-information ESM compatibility issues
const SyntaxHighlighter = dynamic(
  () => import('react-syntax-highlighter/dist/cjs/light').then((mod) => mod.default),
  { ssr: false, loading: () => <PreviewLoading /> }
);

const ReactMarkdown = dynamic(() => import('react-markdown'), {
  ssr: false,
  loading: () => <PreviewLoading />,
});

function PreviewLoading() {
  return (
    <div className="flex items-center justify-center p-8 text-gray-500">
      <svg className="animate-spin h-5 w-5 mr-2" fill="none" viewBox="0 0 24 24">
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
      </svg>
      Loading preview...
    </div>
  );
}

interface FilePreviewProps {
  fileName: string;
  content: string; // Base64 encoded content
  mimeType: string | null;
  onClose: () => void;
}

// Map file extensions to syntax highlighter language names
const EXTENSION_LANGUAGE_MAP: Record<string, string> = {
  // Programming languages
  js: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  py: 'python',
  rb: 'ruby',
  go: 'go',
  rs: 'rust',
  java: 'java',
  kt: 'kotlin',
  swift: 'swift',
  c: 'c',
  cpp: 'cpp',
  h: 'c',
  hpp: 'cpp',
  cs: 'csharp',
  php: 'php',
  r: 'r',
  scala: 'scala',
  // Web
  html: 'html',
  htm: 'html',
  css: 'css',
  scss: 'scss',
  less: 'less',
  // Data formats
  json: 'json',
  xml: 'xml',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'toml',
  csv: 'text',
  // Shell/Config
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  fish: 'bash',
  ps1: 'powershell',
  bat: 'batch',
  cmd: 'batch',
  // Other
  sql: 'sql',
  graphql: 'graphql',
  dockerfile: 'dockerfile',
  makefile: 'makefile',
  md: 'markdown',
  markdown: 'markdown',
  txt: 'text',
  log: 'text',
  env: 'bash',
  gitignore: 'text',
};

// MIME types that are previewable as text
const TEXT_MIME_PREFIXES = ['text/', 'application/json', 'application/xml', 'application/javascript'];

function getFileExtension(fileName: string): string {
  const parts = fileName.toLowerCase().split('.');
  return parts.length > 1 ? parts[parts.length - 1] : '';
}

function isTextPreviewable(mimeType: string | null, fileName: string): boolean {
  // Check mime type
  if (mimeType) {
    if (TEXT_MIME_PREFIXES.some((prefix) => mimeType.startsWith(prefix))) {
      return true;
    }
  }
  // Check extension
  const ext = getFileExtension(fileName);
  return ext in EXTENSION_LANGUAGE_MAP;
}

function isMarkdown(fileName: string): boolean {
  const ext = getFileExtension(fileName);
  return ext === 'md' || ext === 'markdown';
}

function isCsv(fileName: string): boolean {
  return getFileExtension(fileName) === 'csv';
}

function isImage(mimeType: string | null): boolean {
  return mimeType?.startsWith('image/') || false;
}

function decodeBase64Content(base64: string): string {
  try {
    const binaryString = atob(base64);
    // Handle UTF-8 encoded text
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
      bytes[i] = binaryString.charCodeAt(i);
    }
    return new TextDecoder('utf-8').decode(bytes);
  } catch {
    return atob(base64);
  }
}

// CSV Preview component using papaparse for proper CSV parsing
function CsvPreview({ content }: { content: string }) {
  const parsed = Papa.parse<string[]>(content, {
    skipEmptyLines: true,
  });
  const rows = parsed.data;
  const headers = rows[0] || [];
  const dataRows = rows.slice(1);

  return (
    <div className="overflow-auto max-h-[60vh]">
      <table className="min-w-full divide-y divide-gray-200 text-sm">
        <thead className="bg-gray-50 sticky top-0">
          <tr>
            {headers.map((header, i) => (
              <th
                key={i}
                className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wider border-r border-gray-200 last:border-r-0"
              >
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="bg-white divide-y divide-gray-200">
          {dataRows.slice(0, 100).map((row, rowIndex) => (
            <tr key={rowIndex} className="hover:bg-gray-50">
              {row.map((cell, cellIndex) => (
                <td
                  key={cellIndex}
                  className="px-3 py-2 whitespace-nowrap text-gray-700 border-r border-gray-100 last:border-r-0"
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {dataRows.length > 100 && (
        <div className="text-center py-2 text-gray-500 text-sm bg-gray-50">
          Showing first 100 rows of {dataRows.length}
        </div>
      )}
    </div>
  );
}

// Code Preview with syntax highlighting
function CodePreview({ content, language }: { content: string; language: string }) {
  const [style, setStyle] = useState<Record<string, React.CSSProperties> | null>(null);

  useEffect(() => {
    // Dynamically import the style (use CJS build for compatibility)
    import('react-syntax-highlighter/dist/cjs/styles/hljs/github').then((mod) => {
      setStyle(mod.default);
    });
  }, []);

  if (!style) {
    return <PreviewLoading />;
  }

  return (
    <div className="overflow-auto max-h-[60vh] text-sm">
      <SyntaxHighlighter
        language={language}
        style={style}
        showLineNumbers
        wrapLongLines
        customStyle={{ margin: 0, borderRadius: 0 }}
      >
        {content}
      </SyntaxHighlighter>
    </div>
  );
}

// Markdown Preview
function MarkdownPreview({ content }: { content: string }) {
  return (
    <div className="overflow-auto max-h-[60vh] p-6 prose prose-sm max-w-none">
      <ReactMarkdown>{content}</ReactMarkdown>
    </div>
  );
}

// Image Preview
function ImagePreview({ content, mimeType }: { content: string; mimeType: string }) {
  const dataUrl = `data:${mimeType};base64,${content}`;
  return (
    <div className="flex items-center justify-center p-4 bg-gray-100 max-h-[60vh] overflow-auto">
      <img src={dataUrl} alt="Preview" className="max-w-full max-h-[55vh] object-contain" />
    </div>
  );
}

// Plain text fallback
function TextPreview({ content }: { content: string }) {
  return (
    <div className="overflow-auto max-h-[60vh] p-4">
      <pre className="text-sm text-gray-700 whitespace-pre-wrap font-mono">{content}</pre>
    </div>
  );
}

export default function FilePreview({ fileName, content, mimeType, onClose }: FilePreviewProps) {
  const [decodedContent, setDecodedContent] = useState<string | null>(null);
  const ext = getFileExtension(fileName);
  const language = EXTENSION_LANGUAGE_MAP[ext] || 'text';

  useEffect(() => {
    // Don't decode for images - they use base64 directly
    if (!isImage(mimeType)) {
      setDecodedContent(decodeBase64Content(content));
    }
  }, [content, mimeType]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose();
      }
    },
    [onClose]
  );

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  // Determine preview type
  const renderPreview = () => {
    if (isImage(mimeType)) {
      return <ImagePreview content={content} mimeType={mimeType!} />;
    }

    if (!decodedContent) {
      return <PreviewLoading />;
    }

    if (isMarkdown(fileName)) {
      return <MarkdownPreview content={decodedContent} />;
    }

    if (isCsv(fileName)) {
      return <CsvPreview content={decodedContent} />;
    }

    if (isTextPreviewable(mimeType, fileName)) {
      return <CodePreview content={decodedContent} language={language} />;
    }

    // Fallback to plain text
    return <TextPreview content={decodedContent} />;
  };

  // Check if file is previewable
  const canPreview = isImage(mimeType) || isTextPreviewable(mimeType, fileName);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-lg shadow-xl max-w-4xl w-full mx-4 max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200">
          <div className="flex items-center gap-2 min-w-0">
            <svg className="w-5 h-5 text-gray-400 shrink-0" fill="currentColor" viewBox="0 0 20 20">
              <path
                fillRule="evenodd"
                d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4z"
                clipRule="evenodd"
              />
            </svg>
            <h3 className="text-lg font-medium text-gray-900 truncate">{fileName}</h3>
            {mimeType && (
              <span className="text-xs text-gray-500 bg-gray-100 px-2 py-0.5 rounded shrink-0">
                {mimeType}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 rounded-full hover:bg-gray-100"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-hidden">
          {canPreview ? (
            renderPreview()
          ) : (
            <div className="flex flex-col items-center justify-center p-8 text-gray-500">
              <svg className="w-12 h-12 mb-4 text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={1.5}
                  d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                />
              </svg>
              <p>Preview not available for this file type</p>
              <p className="text-sm mt-1">Download the file to view it</p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-3 border-t border-gray-200 bg-gray-50 flex justify-end">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

// Export a helper to check if preview is supported
export function canPreviewFile(mimeType: string | null, fileName: string): boolean {
  return isImage(mimeType) || isTextPreviewable(mimeType, fileName);
}
