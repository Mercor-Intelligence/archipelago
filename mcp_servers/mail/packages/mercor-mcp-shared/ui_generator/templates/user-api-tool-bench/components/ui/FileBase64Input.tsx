// File upload input that converts to base64
// Useful for parameters like file_content_base64 that expect base64-encoded file content

import { useRef, useState } from 'react';

interface FileInfo {
  name: string;
  size: number;
}

interface FileBase64InputProps {
  value: string;
  onChange: (base64: string) => void;
  onFileNameChange?: (fileName: string) => void;
  paramName: string;
  required?: boolean;
  accept?: string;  // File type filter, e.g., ".twb,.twbx" or "image/*"
  maxSizeMB?: number;  // Max file size in MB, default 50
  description?: string;  // Help text shown below the input
}

export default function FileBase64Input({
  value,
  onChange,
  onFileNameChange,
  paramName,
  required,
  accept,
  maxSizeMB = 50,
  description,
}: FileBase64InputProps) {
  const [fileInfo, setFileInfo] = useState<FileInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  // Track file read generation to handle race conditions when user selects new file mid-read
  const fileReadGenerationRef = useRef<number>(0);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setError(null);

    // Reject empty files
    if (file.size === 0) {
      setError('File is empty. Please select a valid file.');
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
      return;
    }

    // Validate file size
    const maxSize = maxSizeMB * 1024 * 1024;
    if (file.size > maxSize) {
      setError(`File too large. Maximum size is ${maxSizeMB}MB. Your file is ${(file.size / 1024 / 1024).toFixed(1)}MB.`);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
      return;
    }

    // Increment generation to invalidate any in-progress reads
    fileReadGenerationRef.current += 1;
    const capturedGeneration = fileReadGenerationRef.current;
    const capturedFileName = file.name;
    const capturedFileSize = file.size;

    // Read file as base64
    const reader = new FileReader();

    reader.onload = () => {
      // Check if file selection changed during read
      if (fileReadGenerationRef.current !== capturedGeneration) {
        console.log('FileReader completed but file selection changed - ignoring stale result');
        return;
      }

      // Result is data URL like "data:application/octet-stream;base64,XXXX"
      // Extract just the base64 part after the comma
      const dataUrl = reader.result as string;
      const base64 = dataUrl.split(',')[1];

      onChange(base64);
      setFileInfo({ name: capturedFileName, size: capturedFileSize });

      // Auto-populate file_name if callback provided
      if (onFileNameChange) {
        onFileNameChange(capturedFileName);
      }
    };

    reader.onerror = () => {
      // Check if file selection changed during read
      if (fileReadGenerationRef.current !== capturedGeneration) {
        return;
      }
      setError('Failed to read file. Please try again.');
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    };

    reader.readAsDataURL(file);
  };

  const handleRemove = () => {
    // Increment generation to invalidate any in-progress file reads
    fileReadGenerationRef.current += 1;
    onChange('');
    setFileInfo(null);
    setError(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes >= 1024 * 1024) {
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    }
    return `${(bytes / 1024).toFixed(1)} KB`;
  };

  return (
    <div className="space-y-3">
      <div className="space-y-2">
        <input
          type="file"
          accept={accept}
          ref={fileInputRef}
          onChange={handleFileSelect}
          className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100"
          data-param-name={paramName}
          required={required && !value}
        />
        {description && (
          <p className="text-xs text-gray-500">{description}</p>
        )}
        {!description && (
          <p className="text-xs text-gray-500">
            Upload a file from your computer. Max size: {maxSizeMB}MB.
          </p>
        )}
      </div>

      {/* Error display */}
      {error && (
        <div className="flex items-center gap-2 p-3 bg-red-50 border border-red-200 rounded text-sm text-red-700">
          <svg className="w-5 h-5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span>{error}</span>
        </div>
      )}

      {/* Success indicator when file is uploaded */}
      {value && fileInfo && (
        <div className="flex items-center justify-between p-3 bg-green-50 border border-green-200 rounded text-sm text-green-700">
          <div className="flex items-center gap-2">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <div>
              <div className="font-medium">{fileInfo.name}</div>
              <div className="text-xs text-green-600">
                {formatFileSize(fileInfo.size)} - Ready to upload
              </div>
            </div>
          </div>
          <button
            type="button"
            onClick={handleRemove}
            className="px-3 py-1 text-red-600 hover:text-red-800 hover:bg-red-50 rounded font-medium transition-colors"
          >
            Remove
          </button>
        </div>
      )}
    </div>
  );
}
