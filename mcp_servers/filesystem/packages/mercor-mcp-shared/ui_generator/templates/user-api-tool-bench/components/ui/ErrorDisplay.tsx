interface ErrorDisplayProps {
  error: string;
}

export default function ErrorDisplay({ error }: ErrorDisplayProps) {
  return (
    <div className="mt-6 rounded-lg border-l-4 border-red-500 bg-red-50 p-4">
      <div className="flex items-start gap-3">
        <svg className="h-5 w-5 text-red-500 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <div className="flex-1">
          <p className="text-sm font-semibold text-red-700">Request Error</p>
          <p className="text-sm text-red-600 mt-1 whitespace-pre-wrap">{error}</p>
        </div>
      </div>
    </div>
  );
}
