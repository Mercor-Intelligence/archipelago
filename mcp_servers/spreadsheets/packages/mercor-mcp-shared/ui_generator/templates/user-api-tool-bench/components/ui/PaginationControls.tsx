export interface PaginationInfo {
  page: number;
  total_pages: number;
  total_rows: number;
  rows_per_page: number;
  has_more: boolean;
  message?: string;
  /** Parameter name the tool uses for page number (e.g. "page" or "page_number") */
  page_param?: string;
  /** Parameter name the tool uses for page size (e.g. "per_page" or "limit") */
  limit_param?: string;
}

interface PaginationControlsProps {
  pagination: PaginationInfo;
  onLoadPage: (page: number) => Promise<void> | void;
  loading?: boolean;
  className?: string;
  /** Visual variant: "light" (default) for white backgrounds, "dark" for dark header bars */
  variant?: 'light' | 'dark';
  /** If true, calls stopPropagation on button clicks (useful inside clickable containers) */
  stopPropagation?: boolean;
}

const styles = {
  light: {
    container: 'flex items-center justify-between px-4 py-3 bg-white border border-gray-200 rounded-lg',
    info: 'text-xs text-gray-500',
    button: 'px-3 py-1.5 text-xs font-medium rounded transition-colors disabled:opacity-40 disabled:cursor-not-allowed bg-white border border-gray-300 text-gray-700 hover:bg-gray-50',
    page: 'text-xs text-gray-600 min-w-[4rem] text-center',
  },
  dark: {
    container: 'flex items-center gap-2 flex-shrink-0',
    info: 'text-xs text-gray-300 whitespace-nowrap',
    button: 'px-2 py-0.5 text-xs rounded transition-colors bg-gray-600 hover:bg-gray-500 disabled:bg-gray-700 disabled:text-gray-500',
    page: 'text-xs text-gray-300 min-w-[4rem] text-center whitespace-nowrap',
  },
};

export default function PaginationControls({
  pagination,
  onLoadPage,
  loading,
  className,
  variant = 'light',
  stopPropagation,
}: PaginationControlsProps) {
  if (pagination.total_pages <= 1) return null;

  const s = styles[variant];

  const handleClick = (page: number) => (e: React.MouseEvent) => {
    if (stopPropagation) e.stopPropagation();
    onLoadPage(page);
  };

  return (
    <div className={className ?? s.container}>
      <div className={s.info}>
        Page {pagination.page} of {pagination.total_pages} ({pagination.total_rows.toLocaleString()} total rows)
      </div>
      <div className="flex items-center gap-2">
        <button
          onClick={handleClick(pagination.page - 1)}
          disabled={pagination.page <= 1 || loading}
          className={s.button}
        >
          Prev
        </button>
        <span className={s.page}>
          {loading ? (
            <span className="inline-flex items-center gap-1">
              <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              Loading
            </span>
          ) : (
            `${pagination.page} / ${pagination.total_pages}`
          )}
        </span>
        <button
          onClick={handleClick(pagination.page + 1)}
          disabled={!pagination.has_more || loading}
          className={s.button}
        >
          Next
        </button>
      </div>
    </div>
  );
}
