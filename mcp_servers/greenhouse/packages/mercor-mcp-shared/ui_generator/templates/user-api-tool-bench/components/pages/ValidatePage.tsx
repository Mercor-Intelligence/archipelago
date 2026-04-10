import React, { useState, useCallback, useRef, useEffect } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import axios from 'axios';
import { getApiBase } from '@mcp-shared/utils/api';
import type { AppConfig, TableSchema, SchemaResponse } from '../types/app-config';

interface ValidatePageProps {
  config: AppConfig;
}

// Types for validation response
interface ValidationError {
  file: string;
  error_type: string;
  message: string;
  row: number | null;
  column: string | null;
}

interface FileValidationResult {
  file_name: string;
  table_name: string | null;
  success: boolean;
  row_count: number;
  errors: ValidationError[];
  sample_rows: Record<string, string>[];
}

interface ValidationResponse {
  success: boolean;
  files_total: number;
  files_valid: number;
  files_invalid: number;
  total_errors: number;
  fk_violations: number;
  files: FileValidationResult[];
  fk_errors: ValidationError[];
}

// Step configuration
const STEPS = [
  { id: 1, title: 'Upload', description: 'Upload your CSV files' },
  { id: 2, title: 'Review', description: 'Check validation results' },
  { id: 3, title: 'Import', description: 'Import to database' },
];

// Error type display info
const ERROR_TYPE_INFO: Record<string, { bg: string; text: string; label: string; icon: string }> = {
  READ_ERROR: { bg: 'bg-red-100', text: 'text-red-800', label: 'File Error', icon: '📄' },
  NO_HEADERS: { bg: 'bg-red-100', text: 'text-red-800', label: 'Missing Headers', icon: '📋' },
  NO_TABLE_MATCH: { bg: 'bg-orange-100', text: 'text-orange-800', label: 'Unknown Table', icon: '❓' },
  NO_CSV_FILES: { bg: 'bg-orange-100', text: 'text-orange-800', label: 'No Files', icon: '📁' },
  MISSING_REQUIRED: { bg: 'bg-yellow-100', text: 'text-yellow-800', label: 'Missing Column', icon: '⚠️' },
  NULL_VALUE: { bg: 'bg-yellow-100', text: 'text-yellow-800', label: 'Empty Value', icon: '⚠️' },
  TYPE_ERROR: { bg: 'bg-purple-100', text: 'text-purple-800', label: 'Wrong Format', icon: '🔢' },
  FK_VIOLATION: { bg: 'bg-blue-100', text: 'text-blue-800', label: 'Missing Reference', icon: '🔗' },
  DUPLICATE_PK: { bg: 'bg-red-100', text: 'text-red-800', label: 'Duplicate ID', icon: '🔄' },
  DUPLICATE_UNIQUE: { bg: 'bg-red-100', text: 'text-red-800', label: 'Duplicate Value', icon: '🔄' },
  INVALID_ENUM: { bg: 'bg-purple-100', text: 'text-purple-800', label: 'Invalid Value', icon: '📝' },
};

// Group errors by type and column
interface GroupedError {
  error_type: string;
  column: string | null;
  refTable?: string;
  missingValues: Set<string>;
  rows: number[];
  originalError: ValidationError;
}

function groupErrors(errors: ValidationError[]): GroupedError[] {
  const groups = new Map<string, GroupedError>();

  errors.forEach(error => {
    let key = `${error.error_type}:${error.column || 'general'}`;
    let refTable: string | undefined;
    let missingValue: string | undefined;

    if (error.error_type === 'FK_VIOLATION') {
      const fkMatch = error.message.match(/Foreign key '(\w+)' value '([^']+)' not found in (\w+)/);
      if (fkMatch) {
        const [, col, value, table] = fkMatch;
        key = `FK:${col}:${table}`;
        refTable = table;
        missingValue = value;
      }
    }

    if (groups.has(key)) {
      const group = groups.get(key)!;
      if (missingValue) group.missingValues.add(missingValue);
      if (error.row !== null) group.rows.push(error.row);
    } else {
      groups.set(key, {
        error_type: error.error_type,
        column: error.column,
        refTable,
        missingValues: new Set(missingValue ? [missingValue] : []),
        rows: error.row !== null ? [error.row] : [],
        originalError: error,
      });
    }
  });

  return Array.from(groups.values());
}

function ErrorTypeBadge({ type }: { type: string }) {
  const info = ERROR_TYPE_INFO[type] || { bg: 'bg-gray-100', text: 'text-gray-800', label: type.replace(/_/g, ' '), icon: '❗' };
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded ${info.bg} ${info.text}`}>
      <span>{info.icon}</span>
      <span>{info.label}</span>
    </span>
  );
}

function SimpleErrorSummary({ group }: { group: GroupedError }) {
  const { error_type, column, refTable, missingValues, rows } = group;

  let message: string;

  if (error_type === 'FK_VIOLATION' && refTable) {
    const count = missingValues.size;
    message = `References ${count} ${refTable.replace(/_/g, ' ')} ID${count > 1 ? 's' : ''} that don't exist`;
  } else if (error_type === 'NULL_VALUE') {
    message = `"${column}" is empty in ${rows.length} row${rows.length > 1 ? 's' : ''}`;
  } else if (error_type === 'TYPE_ERROR') {
    message = `"${column}" has invalid format in ${rows.length} row${rows.length > 1 ? 's' : ''}`;
  } else if (error_type === 'INVALID_ENUM') {
    message = `"${column}" has invalid value in ${rows.length} row${rows.length > 1 ? 's' : ''}`;
  } else if (error_type === 'MISSING_REQUIRED') {
    message = group.originalError.message;
  } else if (error_type === 'NO_TABLE_MATCH') {
    message = `Can't match this file to a known table`;
  } else if (error_type === 'NO_HEADERS') {
    message = `File is empty or missing column headers`;
  } else if (error_type === 'DUPLICATE_PK') {
    message = `Duplicate IDs found - "${column}" must be unique for each row`;
  } else if (error_type === 'DUPLICATE_UNIQUE') {
    message = `Duplicate values found - "${column}" must be unique`;
  } else {
    message = group.originalError.message;
  }

  return (
    <div className="flex items-center gap-2 py-1.5 text-sm">
      <span className="text-red-500">*</span>
      <span className="text-gray-700">{message}</span>
      {rows.length > 0 && (
        <span className="text-gray-400 text-xs">(lines: {rows.slice(0, 5).join(', ')}{rows.length > 5 ? '...' : ''})</span>
      )}
    </div>
  );
}

function FileResultCard({
  result,
  fkErrors = [],
}: {
  result: FileValidationResult;
  fkErrors?: ValidationError[];
}) {
  const allErrors = [...result.errors, ...fkErrors];
  const hasErrors = allErrors.length > 0;
  const isValid = result.success && fkErrors.length === 0;

  return (
    <div className={`border rounded-lg overflow-hidden ${
      isValid ? 'border-green-200 bg-green-50' : 'border-red-200 bg-red-50'
    }`}>
      <div className="px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          {isValid ? (
            <svg className="w-5 h-5 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
            </svg>
          ) : (
            <svg className="w-5 h-5 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          )}
          <div>
            <span className="font-medium text-gray-900">{result.file_name}</span>
            {result.table_name && (
              <span className="ml-2 text-sm text-gray-500">-&gt; {result.table_name}</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-4 text-sm">
          <span className="text-gray-600">{result.row_count} rows</span>
          {hasErrors && (
            <span className="text-red-600 font-medium">{allErrors.length} issue{allErrors.length > 1 ? 's' : ''}</span>
          )}
        </div>
      </div>

      {hasErrors && (
        <div className="border-t border-red-200 bg-white px-4 py-3">
          {groupErrors(allErrors).map((group, idx) => (
            <SimpleErrorSummary key={idx} group={group} />
          ))}
        </div>
      )}
    </div>
  );
}

function generateDetailedErrorReport(result: ValidationResponse, schema: TableSchema[]): string {
  const lines: string[] = [];

  lines.push('=== CSV VALIDATION ERROR REPORT ===');
  lines.push('');
  lines.push('I need help fixing validation errors in my CSV data files.');
  lines.push('');
  lines.push('SUMMARY:');
  lines.push(`- Total files: ${result.files_total}`);
  lines.push(`- Files with errors: ${result.files_invalid}`);
  lines.push(`- Total errors: ${result.total_errors + result.fk_violations}`);
  lines.push('');

  const fkErrorsByFile = new Map<string, ValidationError[]>();
  result.fk_errors.forEach(e => {
    const existing = fkErrorsByFile.get(e.file) || [];
    existing.push(e);
    fkErrorsByFile.set(e.file, existing);
  });

  result.files.forEach(file => {
    const fileFkErrors = fkErrorsByFile.get(file.file_name) || [];
    const allErrors = [...file.errors, ...fileFkErrors];

    if (allErrors.length === 0) return;

    lines.push('---');
    lines.push(`FILE: ${file.file_name}`);
    lines.push(`Table: ${file.table_name || 'unknown'}`);
    lines.push(`Rows: ${file.row_count}`);
    lines.push('');

    const grouped = groupErrors(allErrors);

    grouped.forEach((group, idx) => {
      lines.push(`Error ${idx + 1}: ${group.error_type}`);
      if (group.column) lines.push(`  Column: ${group.column}`);
      if (group.refTable) lines.push(`  References table: ${group.refTable}`);
      if (group.rows.length > 0) {
        const rowList = group.rows.length <= 10
          ? group.rows.join(', ')
          : `${group.rows.slice(0, 10).join(', ')}... (${group.rows.length} total)`;
        lines.push(`  CSV line numbers: ${rowList} (line 1 is header, line 2 is first data row)`);
      }
      if (group.missingValues.size > 0) {
        const vals = Array.from(group.missingValues);
        const valList = vals.length <= 10
          ? vals.join(', ')
          : `${vals.slice(0, 10).join(', ')}... (${vals.length} total)`;
        lines.push(`  Missing values: ${valList}`);
      }
      lines.push(`  Original message: ${group.originalError.message}`);
      lines.push('');
    });
  });

  lines.push('---');
  lines.push('');
  lines.push('Please help me understand what is wrong and how to fix my CSV files.');
  lines.push('');

  if (schema && schema.length > 0) {
    lines.push('=== DATABASE SCHEMA REFERENCE ===');
    lines.push('');
    lines.push('The following tables are expected. Columns marked with * are required.');
    lines.push('Foreign keys (FK) must reference existing IDs in the target table.');
    lines.push('');

    schema.forEach(table => {
      lines.push(`TABLE: ${table.name}`);
      lines.push('Columns:');
      table.columns.forEach(col => {
        const markers: string[] = [];
        if (col.is_primary_key) markers.push('PK');
        if (col.is_foreign_key && col.fk_target) markers.push(`FK -> ${col.fk_target}`);
        if (col.required) markers.push('required');

        const markerStr = markers.length > 0 ? ` (${markers.join(', ')})` : '';
        const nullableStr = col.nullable ? '' : '*';
        lines.push(`  - ${col.name}${nullableStr}: ${col.type}${markerStr}`);
      });
      if (table.required_columns.length > 0) {
        lines.push(`Required columns for CSV: ${table.required_columns.join(', ')}`);
      }
      lines.push('');
    });
  }

  return lines.join('\n');
}

// Progress indicator component
function ProgressIndicator({ currentStep, steps }: { currentStep: number; steps: typeof STEPS }) {
  return (
    <div className="flex items-center justify-center gap-2">
      {steps.map((step, index) => {
        const isCompleted = currentStep > step.id;
        const isCurrent = currentStep === step.id;

        return (
          <div key={step.id} className="flex items-center">
            {/* Step indicator */}
            <div className="flex flex-col items-center">
              <div
                className={`w-10 h-10 rounded-full flex items-center justify-center text-sm font-semibold transition-all duration-300 ${
                  isCompleted
                    ? 'bg-green-600 text-white'
                    : isCurrent
                    ? 'bg-gray-900 text-white ring-4 ring-gray-200'
                    : 'bg-gray-200 text-gray-500'
                }`}
              >
                {isCompleted ? (
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                  </svg>
                ) : (
                  step.id
                )}
              </div>
              <span
                className={`mt-2 text-xs font-medium transition-colors duration-300 ${
                  isCurrent ? 'text-gray-900' : isCompleted ? 'text-green-600' : 'text-gray-400'
                }`}
              >
                {step.title}
              </span>
            </div>

            {/* Connector line */}
            {index < steps.length - 1 && (
              <div
                className={`w-16 h-1 mx-2 rounded transition-colors duration-300 ${
                  currentStep > step.id ? 'bg-green-600' : 'bg-gray-200'
                }`}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function ValidatePage({ config }: ValidatePageProps) {
  const [currentStep, setCurrentStep] = useState(1);
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ValidationResponse | null>(null);
  const [schema, setSchema] = useState<TableSchema[]>([]);
  const [importing, setImporting] = useState(false);
  const [importSuccess, setImportSuccess] = useState(false);
  const [validatedFileName, setValidatedFileName] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [showCopyModal, setShowCopyModal] = useState(false);
  const [expandedFile, setExpandedFile] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const copyTextareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const fetchSchema = async () => {
      try {
        const apiBase = getApiBase();
        const response = await axios.get<SchemaResponse>(`${apiBase}/schema`);
        setSchema(response.data.tables);
        setError(null);
      } catch (err) {
        console.error('Failed to fetch schema:', err);
        setError('Failed to load database schema. Please check that the server is running.');
      }
    };
    fetchSchema();
  }, []);

  const handleCopyErrors = useCallback(async () => {
    if (!result) return;
    const report = generateDetailedErrorReport(result, schema);

    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(report);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
        return;
      }
    } catch (err) {
      console.error('Clipboard API failed:', err);
    }

    setShowCopyModal(true);
  }, [result, schema]);

  const handleCopyFromModal = useCallback(() => {
    if (copyTextareaRef.current) {
      copyTextareaRef.current.select();
      document.execCommand('copy');
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, []);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = e.target.files?.[0];
    if (selectedFile) {
      setFile(selectedFile);
      setError(null);
      setResult(null);
      setImportSuccess(false);
      setValidatedFileName(null);
      setExpandedFile(null);
    }
  }, []);

  const handleDrop = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    const droppedFile = e.dataTransfer.files[0];
    if (droppedFile && droppedFile.name.endsWith('.zip')) {
      setFile(droppedFile);
      setError(null);
      setResult(null);
      setImportSuccess(false);
      setValidatedFileName(null);
      setExpandedFile(null);
    } else {
      setError('Please upload a ZIP file');
    }
  }, []);

  const handleValidate = useCallback(async () => {
    if (!file) return;

    const fileBeingValidated = file;
    setLoading(true);
    setError(null);
    setResult(null);
    setImportSuccess(false);
    setValidatedFileName(null);

    try {
      const base64Content = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const result = reader.result as string;
          const base64 = result.split(',')[1];
          resolve(base64);
        };
        reader.onerror = () => reject(new Error('Failed to read file'));
        reader.readAsDataURL(fileBeingValidated);
      });

      const apiBase = getApiBase();
      const response = await axios.post<ValidationResponse>(
        `${apiBase}/validate`,
        { file_content: base64Content, filename: fileBeingValidated.name }
      );

      setResult(response.data);
      setValidatedFileName(fileBeingValidated.name);
      // Move to step 2 after successful validation
      setCurrentStep(2);
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || err.message || 'Validation failed';
      setError(errorMessage);
    } finally {
      setLoading(false);
    }
  }, [file]);

  const handleImport = useCallback(async () => {
    if (!file || !result?.success || file.name !== validatedFileName) return;

    setImporting(true);
    setError(null);

    try {
      const base64Content = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          const result = reader.result as string;
          const base64 = result.split(',')[1];
          resolve(base64);
        };
        reader.onerror = () => reject(new Error('Failed to read file'));
        reader.readAsDataURL(file);
      });

      const apiBase = getApiBase();
      await axios.post(`${apiBase}/import-validated`, {
        file_content: base64Content,
        filename: file.name,
      });

      setImportSuccess(true);
    } catch (err: any) {
      const errorMessage = err.response?.data?.detail || err.message || 'Import failed';
      setError(errorMessage);
    } finally {
      setImporting(false);
    }
  }, [file, result, validatedFileName]);

  const handleClear = useCallback(() => {
    setFile(null);
    setResult(null);
    setError(null);
    setImportSuccess(false);
    setValidatedFileName(null);
    setExpandedFile(null);
    setCurrentStep(1);
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  }, []);

  const goToStep = useCallback((step: number) => {
    // Can only go back, not forward (unless conditions are met)
    if (step < currentStep) {
      setCurrentStep(step);
    }
  }, [currentStep]);

  const proceedToImport = useCallback(() => {
    if (result?.success) {
      setCurrentStep(3);
    }
  }, [result]);

  // Determine if we can proceed from current step
  const canProceedFromStep1 = file !== null;

  return (
    <>
      <Head>
        <title>Validate CSV Data - {config.name}</title>
        <meta name="description" content={`Validate CSV files before importing into ${config.name}`} />
      </Head>

      <div className="min-h-screen bg-gray-50">
        {/* Header */}
        <header className="bg-white border-b border-gray-200 sticky top-0 z-10">
          <div className="max-w-4xl mx-auto px-4 py-4">
            <div className="flex items-center justify-between">
              <div>
                <h1 className="text-xl font-semibold text-gray-900">Validate & Import Data</h1>
                <p className="text-sm text-gray-500">Upload, validate, and import your CSV files</p>
              </div>
              <Link href="/" className="text-sm text-blue-600 hover:text-blue-800 flex items-center gap-1">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
                </svg>
                Back to GUI
              </Link>
            </div>
          </div>
        </header>

        {/* Progress Indicator */}
        <div className="bg-white border-b border-gray-200 py-6">
          <div className="max-w-4xl mx-auto px-4">
            <ProgressIndicator currentStep={currentStep} steps={STEPS} />
          </div>
        </div>

        {/* Error display */}
        {error && (
          <div className="max-w-4xl mx-auto px-4 pt-6">
            <div className="rounded-lg border-l-4 border-red-500 bg-red-50 p-4">
              <div className="flex items-start gap-3">
                <svg className="h-5 w-5 text-red-500 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <div className="flex-1">
                  <p className="text-sm font-semibold text-red-700">Error</p>
                  <p className="text-sm text-red-600 mt-1">{error}</p>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Carousel Container */}
        <main className="max-w-4xl mx-auto px-4 py-8">
          <div className="overflow-hidden">
            <div
              className="flex transition-transform duration-500 ease-in-out"
              style={{ transform: `translateX(-${(currentStep - 1) * 100}%)` }}
            >
              {/* Step 1: Upload */}
              <div className="w-full flex-shrink-0 px-1">
                <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                  <div className="px-8 py-6 border-b border-gray-100 bg-gradient-to-r from-gray-50 to-white">
                    <h2 className="text-2xl font-bold text-gray-900">Upload Your Files</h2>
                    <p className="mt-2 text-gray-600">
                      Select a ZIP file containing your CSV data files. We'll validate them against the database schema.
                    </p>
                  </div>
                  <div className="p-8">
                    <div
                      onDrop={handleDrop}
                      onDragOver={(e) => e.preventDefault()}
                      onClick={() => fileInputRef.current?.click()}
                      className={`border-2 border-dashed rounded-xl p-12 text-center cursor-pointer transition-all duration-200 ${
                        file
                          ? 'border-green-300 bg-green-50 hover:bg-green-100'
                          : 'border-gray-300 hover:border-blue-400 hover:bg-blue-50'
                      }`}
                    >
                      <input ref={fileInputRef} type="file" accept=".zip" onChange={handleFileSelect} className="hidden" />

                      {file ? (
                        <div className="flex flex-col items-center gap-4">
                          <div className="w-16 h-16 bg-green-100 rounded-full flex items-center justify-center">
                            <svg className="w-8 h-8 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                          </div>
                          <div>
                            <p className="font-semibold text-gray-900 text-lg">{file.name}</p>
                            <p className="text-sm text-gray-500 mt-1">{(file.size / 1024).toFixed(1)} KB</p>
                          </div>
                          <button
                            onClick={(e) => { e.stopPropagation(); handleClear(); }}
                            className="text-sm text-gray-500 hover:text-gray-700 underline"
                          >
                            Choose a different file
                          </button>
                        </div>
                      ) : (
                        <div className="flex flex-col items-center gap-4">
                          <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center">
                            <svg className="w-8 h-8 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12" />
                            </svg>
                          </div>
                          <div>
                            <p className="text-gray-700 text-lg">
                              Drop a ZIP file here or <span className="text-blue-600 font-semibold">browse</span>
                            </p>
                            <p className="mt-2 text-sm text-gray-500">Your ZIP should contain one or more CSV files</p>
                          </div>
                        </div>
                      )}
                    </div>

                    <div className="mt-8 flex justify-end">
                      <button
                        onClick={handleValidate}
                        disabled={!canProceedFromStep1 || loading}
                        className={`inline-flex items-center gap-2 px-8 py-3 rounded-lg font-semibold text-lg transition-all duration-200 ${
                          !canProceedFromStep1 || loading
                            ? 'bg-gray-200 text-gray-400 cursor-not-allowed'
                            : 'bg-gray-900 hover:bg-gray-800 text-white shadow-lg hover:shadow-xl'
                        }`}
                      >
                        {loading ? (
                          <>
                            <svg className="animate-spin w-5 h-5" fill="none" viewBox="0 0 24 24">
                              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                            </svg>
                            Validating...
                          </>
                        ) : (
                          <>
                            Validate Files
                            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14 5l7 7m0 0l-7 7m7-7H3" />
                            </svg>
                          </>
                        )}
                      </button>
                    </div>
                  </div>
                </div>
              </div>

              {/* Step 2: Review Results */}
              <div className="w-full flex-shrink-0 px-1">
                <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                  <div className="px-8 py-6 border-b border-gray-100 bg-gradient-to-r from-gray-50 to-white">
                    <h2 className="text-2xl font-bold text-gray-900">Review Validation Results</h2>
                    <p className="mt-2 text-gray-600">
                      Check the results below. Fix any errors before proceeding to import.
                    </p>
                  </div>
                  <div className="p-8">
                    {result && (
                      <div className="space-y-6">
                        {/* Summary Card */}
                        <div className={`rounded-xl p-6 ${result.success ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'}`}>
                          <div className="flex items-center gap-4">
                            {result.success ? (
                              <div className="w-14 h-14 bg-green-100 rounded-full flex items-center justify-center">
                                <svg className="w-8 h-8 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                                </svg>
                              </div>
                            ) : (
                              <div className="w-14 h-14 bg-red-100 rounded-full flex items-center justify-center">
                                <svg className="w-8 h-8 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z" />
                                </svg>
                              </div>
                            )}
                            <div>
                              <p className={`text-xl font-bold ${result.success ? 'text-green-800' : 'text-red-800'}`}>
                                {result.success ? 'All Files Valid!' : 'Validation Failed'}
                              </p>
                              <p className={`text-sm ${result.success ? 'text-green-700' : 'text-red-700'}`}>
                                {result.success
                                  ? 'Your data is ready to be imported'
                                  : 'Some files have issues that need to be fixed'}
                              </p>
                            </div>
                          </div>

                          <div className="mt-6 grid grid-cols-3 gap-4">
                            <div className="bg-white/60 rounded-lg p-4 text-center">
                              <p className="text-3xl font-bold text-gray-900">{result.files_total}</p>
                              <p className="text-sm text-gray-600">Total Files</p>
                            </div>
                            <div className="bg-white/60 rounded-lg p-4 text-center">
                              <p className="text-3xl font-bold text-green-600">{result.files_valid}</p>
                              <p className="text-sm text-gray-600">Valid</p>
                            </div>
                            <div className="bg-white/60 rounded-lg p-4 text-center">
                              <p className="text-3xl font-bold text-red-600">{result.files_invalid}</p>
                              <p className="text-sm text-gray-600">With Issues</p>
                            </div>
                          </div>
                        </div>

                        {/* File Details (collapsible) */}
                        <details className="group border border-gray-200 rounded-lg overflow-hidden">
                          <summary className="px-4 py-3 cursor-pointer flex items-center justify-between bg-gray-50 hover:bg-gray-100 transition-colors">
                            <span className="font-semibold text-gray-900">File Details</span>
                            <svg className="w-5 h-5 text-gray-400 group-open:rotate-180 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                            </svg>
                          </summary>
                          <div className="p-4 space-y-3 bg-white">
                            {result.files.map((fileResult, idx) => {
                              const fileFkErrors = result.fk_errors.filter(e => e.file === fileResult.file_name);
                              return <FileResultCard key={idx} result={fileResult} fkErrors={fileFkErrors} />;
                            })}
                          </div>
                        </details>

                        {/* Next Steps Workflow (if failed) */}
                        {!result.success && (
                          <div className="bg-blue-50 rounded-lg p-6 border border-blue-200">
                            <h3 className="text-lg font-semibold text-gray-900 mb-4">Next Steps</h3>
                            <div className="space-y-4">
                              {/* Step 1 */}
                              <div className="flex gap-4">
                                <div className="flex-shrink-0 w-7 h-7 bg-blue-600 text-white rounded-full flex items-center justify-center text-sm font-semibold">1</div>
                                <div className="flex-1">
                                  <p className="font-medium text-gray-900">Copy the error report</p>
                                  <p className="text-sm text-gray-600 mt-1">This includes the errors and database schema to help fix your data</p>
                                  <button
                                    onClick={handleCopyErrors}
                                    className="mt-3 inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
                                  >
                                    {copied ? (
                                      <>
                                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                                        </svg>
                                        Copied!
                                      </>
                                    ) : (
                                      <>
                                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                                        </svg>
                                        Copy error report
                                      </>
                                    )}
                                  </button>
                                </div>
                              </div>
                              {/* Step 2 */}
                              <div className="flex gap-4">
                                <div className="flex-shrink-0 w-7 h-7 bg-gray-300 text-gray-700 rounded-full flex items-center justify-center text-sm font-semibold">2</div>
                                <div className="flex-1">
                                  <p className="font-medium text-gray-900">Paste into ChatGPT</p>
                                  <p className="text-sm text-gray-600 mt-1">Ask ChatGPT to fix the errors in your CSV files</p>
                                </div>
                              </div>
                              {/* Step 3 */}
                              <div className="flex gap-4">
                                <div className="flex-shrink-0 w-7 h-7 bg-gray-300 text-gray-700 rounded-full flex items-center justify-center text-sm font-semibold">3</div>
                                <div className="flex-1">
                                  <p className="font-medium text-gray-900">Download the corrected ZIP</p>
                                  <p className="text-sm text-gray-600 mt-1">ChatGPT will provide corrected CSV files to download</p>
                                </div>
                              </div>
                              {/* Step 4 */}
                              <div className="flex gap-4">
                                <div className="flex-shrink-0 w-7 h-7 bg-gray-300 text-gray-700 rounded-full flex items-center justify-center text-sm font-semibold">4</div>
                                <div className="flex-1">
                                  <p className="font-medium text-gray-900">Upload the corrected file</p>
                                  <p className="text-sm text-gray-600 mt-1">Return here and upload the new ZIP to validate again</p>
                                </div>
                              </div>
                            </div>
                          </div>
                        )}
                      </div>
                    )}

                    {/* Navigation buttons */}
                    <div className="mt-8 flex justify-between items-center">
                      {/* Back button - emphasized when validation fails, minimal otherwise */}
                      {result?.success ? (
                        <button
                          onClick={() => goToStep(1)}
                          className="inline-flex items-center gap-2 text-gray-600 hover:text-gray-900 transition-colors"
                        >
                          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
                          </svg>
                          Back to Upload
                        </button>
                      ) : (
                        <button
                          onClick={handleClear}
                          className="inline-flex items-center gap-2 px-6 py-3 rounded-lg font-semibold bg-gray-900 hover:bg-gray-800 text-white transition-colors"
                        >
                          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
                          </svg>
                          Upload Corrected File
                        </button>
                      )}

                      {/* Forward button - only shown when validation succeeds */}
                      {result?.success && (
                        <button
                          onClick={proceedToImport}
                          className="inline-flex items-center gap-2 px-8 py-3 rounded-lg font-semibold text-lg bg-gray-900 hover:bg-gray-800 text-white shadow-lg hover:shadow-xl transition-all duration-200"
                        >
                          Continue to Import
                          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M14 5l7 7m0 0l-7 7m7-7H3" />
                          </svg>
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              </div>

              {/* Step 3: Import */}
              <div className="w-full flex-shrink-0 px-1">
                <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                  <div className="px-8 py-6 border-b border-gray-100 bg-gradient-to-r from-gray-50 to-white">
                    <h2 className="text-2xl font-bold text-gray-900">Import Your Data</h2>
                    <p className="mt-2 text-gray-600">
                      Your files have been validated. Click below to import them into the database.
                    </p>
                  </div>
                  <div className="p-8">
                    {importSuccess ? (
                      <div className="text-center py-12">
                        <div className="w-20 h-20 bg-green-100 rounded-full flex items-center justify-center mx-auto">
                          <svg className="w-10 h-10 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                        </div>
                        <h3 className="mt-6 text-2xl font-bold text-gray-900">Import Complete!</h3>
                        <p className="mt-2 text-gray-600">Your data has been successfully imported to the database.</p>

                        <div className="mt-8 flex justify-center gap-4">
                          <Link
                            href="/"
                            className="inline-flex items-center gap-2 px-6 py-3 rounded-lg font-medium text-white bg-gray-900 hover:bg-gray-800 transition-colors"
                          >
                            Back to GUI
                          </Link>
                          <button
                            onClick={handleClear}
                            className="inline-flex items-center gap-2 px-6 py-3 rounded-lg font-medium text-gray-600 border border-gray-300 hover:bg-gray-50 transition-colors"
                          >
                            Import More Data
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="py-8">
                        {/* Review Final Tables */}
                        <details className="group mb-8 border border-gray-200 rounded-lg overflow-hidden">
                          <summary className="px-4 py-3 cursor-pointer flex items-center justify-between bg-gray-50 hover:bg-gray-100 transition-colors">
                            <span className="font-semibold text-gray-900">Review Final Tables</span>
                            <svg className="w-5 h-5 text-gray-400 group-open:rotate-180 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                            </svg>
                          </summary>
                          <div className="bg-white">
                            <table className="w-full text-sm" style={{ tableLayout: 'fixed' }}>
                              <colgroup>
                                <col style={{ width: '32px' }} />
                                <col style={{ width: '40%' }} />
                                <col style={{ width: '40%' }} />
                                <col style={{ width: '80px' }} />
                              </colgroup>
                              <thead className="bg-gray-50 border-b border-gray-200">
                                <tr>
                                  <th className="px-4 py-2 text-left font-medium text-gray-600"></th>
                                  <th className="px-4 py-2 text-left font-medium text-gray-600">File</th>
                                  <th className="px-4 py-2 text-left font-medium text-gray-600">Table</th>
                                  <th className="px-4 py-2 text-right font-medium text-gray-600">Rows</th>
                                </tr>
                              </thead>
                              <tbody className="divide-y divide-gray-100">
                                {result?.files.map((file, idx) => {
                                  const isExpanded = expandedFile === file.file_name;
                                  const columns = file.sample_rows?.[0] ? Object.keys(file.sample_rows[0]) : [];
                                  return (
                                    <React.Fragment key={idx}>
                                      <tr
                                        className="hover:bg-gray-50 cursor-pointer"
                                        onClick={() => setExpandedFile(isExpanded ? null : file.file_name)}
                                      >
                                        <td className="px-4 py-2">
                                          <svg
                                            className={`w-4 h-4 text-gray-400 transition-transform ${isExpanded ? 'rotate-90' : ''}`}
                                            fill="none"
                                            stroke="currentColor"
                                            viewBox="0 0 24 24"
                                          >
                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                                          </svg>
                                        </td>
                                        <td className="px-4 py-2 text-gray-900 truncate">{file.file_name}</td>
                                        <td className="px-4 py-2 text-gray-600 truncate">{file.table_name || '—'}</td>
                                        <td className="px-4 py-2 text-right text-gray-600">{file.row_count}</td>
                                      </tr>
                                      {isExpanded && file.sample_rows && file.sample_rows.length > 0 && (
                                        <tr>
                                          <td colSpan={4} className="p-0 bg-gray-50">
                                            <div className="px-4 py-2 text-xs text-gray-500">Preview (first {file.sample_rows.length} rows)</div>
                                            <div className="overflow-x-auto px-4 pb-3">
                                              <table className="text-xs border border-gray-200 rounded" style={{ minWidth: '100%', width: 'max-content' }}>
                                                <thead className="bg-gray-100">
                                                  <tr>
                                                    {columns.map((col, i) => (
                                                      <th key={i} className="px-3 py-2 text-left font-medium text-gray-600 border-b border-gray-200 whitespace-nowrap">
                                                        {col}
                                                      </th>
                                                    ))}
                                                  </tr>
                                                </thead>
                                                <tbody>
                                                  {file.sample_rows.map((row, rowIdx) => (
                                                    <tr key={rowIdx} className={rowIdx % 2 === 0 ? 'bg-white' : 'bg-gray-50'}>
                                                      {columns.map((col, colIdx) => (
                                                        <td key={colIdx} className="px-3 py-2 text-gray-700 border-b border-gray-100 whitespace-nowrap">
                                                          {row[col] || '—'}
                                                        </td>
                                                      ))}
                                                    </tr>
                                                  ))}
                                                </tbody>
                                              </table>
                                            </div>
                                          </td>
                                        </tr>
                                      )}
                                    </React.Fragment>
                                  );
                                })}
                              </tbody>
                            </table>
                          </div>
                        </details>

                        <div className="text-center">
                          <button
                            onClick={handleImport}
                            disabled={importing}
                            className={`inline-flex items-center gap-2 px-8 py-3 rounded-lg font-semibold text-lg transition-all duration-200 ${
                              importing
                                ? 'bg-gray-300 text-gray-500 cursor-not-allowed'
                                : 'bg-gray-900 hover:bg-gray-800 text-white shadow-lg hover:shadow-xl'
                            }`}
                          >
                            {importing ? (
                              <>
                                <svg className="animate-spin w-5 h-5" fill="none" viewBox="0 0 24 24">
                                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                                </svg>
                                Importing...
                              </>
                            ) : (
                              'Click to Import Data'
                            )}
                          </button>
                        </div>

                        <div className="mt-8">
                          <button
                            onClick={() => goToStep(2)}
                            className="inline-flex items-center gap-2 text-gray-500 hover:text-gray-700 transition-colors"
                          >
                            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
                            </svg>
                            Back to Review
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>

          {/* Help section */}
          <div className="mt-8 bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <details className="group">
              <summary className="px-6 py-4 cursor-pointer flex items-center justify-between hover:bg-gray-50">
                <span className="font-medium text-gray-900">What gets validated?</span>
                <svg className="w-5 h-5 text-gray-400 group-open:rotate-180 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </summary>
              <div className="px-6 pb-6">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-2">
                  <div className="space-y-3">
                    <h3 className="text-sm font-medium text-gray-700">File-Level Checks</h3>
                    <ul className="space-y-2 text-sm text-gray-600">
                      <li className="flex items-start gap-2">
                        <ErrorTypeBadge type="NO_TABLE_MATCH" />
                        <span>Headers must match a table</span>
                      </li>
                      <li className="flex items-start gap-2">
                        <ErrorTypeBadge type="MISSING_REQUIRED" />
                        <span>Required columns present</span>
                      </li>
                    </ul>
                  </div>
                  <div className="space-y-3">
                    <h3 className="text-sm font-medium text-gray-700">Row-Level Checks</h3>
                    <ul className="space-y-2 text-sm text-gray-600">
                      <li className="flex items-start gap-2">
                        <ErrorTypeBadge type="TYPE_ERROR" />
                        <span>Correct data types</span>
                      </li>
                      <li className="flex items-start gap-2">
                        <ErrorTypeBadge type="FK_VIOLATION" />
                        <span>Valid references</span>
                      </li>
                      <li className="flex items-start gap-2">
                        <ErrorTypeBadge type="INVALID_ENUM" />
                        <span>Valid enum values</span>
                      </li>
                      <li className="flex items-start gap-2">
                        <ErrorTypeBadge type="DUPLICATE_UNIQUE" />
                        <span>No duplicate values</span>
                      </li>
                    </ul>
                  </div>
                </div>
                <div className="mt-4 pt-4 border-t border-gray-200">
                  <p className="text-sm text-gray-500">
                    Need help generating test data?{' '}
                    <Link href="/data-generator" className="text-blue-600 hover:underline">Use the Data Generator</Link>
                  </p>
                </div>
              </div>
            </details>
          </div>
        </main>

        {/* Copy modal fallback */}
        {showCopyModal && result && (
          <div className="fixed inset-0 bg-black bg-opacity-50 z-50 flex items-center justify-center p-4">
            <div className="bg-white rounded-lg shadow-xl max-w-2xl w-full max-h-[80vh] flex flex-col">
              <div className="px-6 py-4 border-b border-gray-200 flex items-center justify-between">
                <h3 className="text-lg font-semibold text-gray-900">Copy Error Details</h3>
                <button onClick={() => setShowCopyModal(false)} className="p-2 text-gray-400 hover:text-gray-600">
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
              <div className="p-4 flex-1 overflow-hidden">
                <textarea
                  ref={copyTextareaRef}
                  readOnly
                  value={generateDetailedErrorReport(result, schema)}
                  className="w-full h-64 p-3 text-sm font-mono bg-gray-50 border border-gray-200 rounded resize-none"
                  onClick={(e) => (e.target as HTMLTextAreaElement).select()}
                />
              </div>
              <div className="px-6 py-4 border-t border-gray-200 flex justify-end gap-3">
                <button onClick={handleCopyFromModal} className="px-4 py-2 bg-gray-900 text-white rounded-lg hover:bg-gray-800">
                  {copied ? 'Copied!' : 'Copy'}
                </button>
                <button onClick={() => setShowCopyModal(false)} className="px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50">
                  Close
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  );
}

export type { ValidatePageProps };
