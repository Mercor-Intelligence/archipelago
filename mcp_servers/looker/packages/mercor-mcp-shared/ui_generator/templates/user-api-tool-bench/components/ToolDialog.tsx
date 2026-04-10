// Modal dialog for executing a single tool.
// Used by Header to open login and database tools without navigating to the Tools tab.
import { useEffect } from 'react';
import { DataType, AuthUser } from '@/lib/api-config';
import ToolForm from '@mcp-shared/ToolForm';

interface ToolDialogProps {
  dataType: DataType | null;
  onClose: () => void;
  token: string;
  onLogin?: (token: string, user: AuthUser) => void;
  onLogout?: () => void;
}

export default function ToolDialog({ dataType, onClose, token, onLogin, onLogout }: ToolDialogProps) {
  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    if (dataType) {
      document.addEventListener('keydown', handleKeyDown);
      return () => document.removeEventListener('keydown', handleKeyDown);
    }
  }, [dataType, onClose]);

  if (!dataType) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-16 px-4">
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black/40" onClick={onClose} />

      {/* Dialog panel */}
      <div className="relative bg-white rounded-xl shadow-2xl w-full max-w-2xl max-h-[80vh] overflow-y-auto">
        {/* Header */}
        <div className="sticky top-0 bg-white border-b border-gray-200 px-6 py-4 flex items-center justify-between rounded-t-xl z-10">
          <h2 className="text-lg font-semibold text-gray-900">
            {dataType.displayName || dataType.name}
          </h2>
          <button
            onClick={onClose}
            className="p-1 text-gray-400 hover:text-gray-600 transition-colors"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Tool form content */}
        <div className="p-6">
          <ToolForm
            dataType={dataType}
            token={token}
            onLogin={onLogin}
            onLogout={onLogout}
          />
        </div>
      </div>
    </div>
  );
}
