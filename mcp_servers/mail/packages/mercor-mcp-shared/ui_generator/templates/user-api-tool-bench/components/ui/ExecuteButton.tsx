interface ExecuteButtonProps {
  onClick: () => void;
  loading: boolean;
  label?: string;
}

export default function ExecuteButton({ onClick, loading, label = 'Execute' }: ExecuteButtonProps) {
  return (
    <button
      onClick={onClick}
      disabled={loading}
      data-testid="execute-tool-button"
      className="w-full bg-indigo-600 text-white py-3 px-4 rounded-lg hover:bg-indigo-700 disabled:bg-gray-400 disabled:cursor-not-allowed font-medium text-sm shadow-sm transition-colors"
    >
      {loading ? (
        <span className="flex items-center justify-center gap-2">
          <svg className="animate-spin h-4 w-4" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
          Executing...
        </span>
      ) : (
        label
      )}
    </button>
  );
}
