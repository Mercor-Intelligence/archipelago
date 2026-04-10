// Dynamic Select component for parameters with populateFrom
// Fetches options from another tool's response
import { useState, useEffect, useRef } from 'react';
import { DataType, dataTypes, getToolEndpoint } from '@/lib/api-config';
import { makeToolRequest, isRequestCanceled } from '@mcp-shared/utils/api';

interface DynamicSelectProps {
  value: string | string[];
  onChange: (value: string | number | string[] | number[]) => void;
  populateFrom: string;
  populateField: string;
  populateValue?: string;
  populateDisplay?: string;
  paramName: string;
  paramType?: string;
  token?: string | null;
  required?: boolean;
  placeholder?: string;
  className?: string;
  multiple?: boolean; // Support multi-select for list parameters
  // Dependencies: map from tool parameter name to form field name or const value
  // e.g., { "job_id": "job_id" } means pass form's job_id value as job_id param
  // e.g., { "table_name": {"const": "cost_centers"} } means pass literal value "cost_centers"
  populateDependencies?: Record<string, string | { const: any }>;
  // Current form values for dependency lookup
  formValues?: Record<string, any>;
}

// Icon components for reusability
function ListIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 10h16M4 14h16M4 18h16" />
    </svg>
  );
}

function PencilIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
    </svg>
  );
}

function RefreshIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
    </svg>
  );
}

export default function DynamicSelect({
  value,
  onChange,
  populateFrom,
  populateField,
  populateValue,
  populateDisplay,
  paramName,
  paramType,
  token,
  required,
  placeholder,
  className = '',
  multiple = false,
  populateDependencies,
  formValues = {},
}: DynamicSelectProps) {
  const [options, setOptions] = useState<{ value: string; display: string }[]>([]);
  const [loading, setLoading] = useState(true); // Start with loading to avoid empty state flash
  const [error, setError] = useState<string | null>(null);
  const [manualMode, setManualMode] = useState(false);
  const [retryCount, setRetryCount] = useState(0);
  const isInitialMount = useRef(true);
  const prevDependencyDataKey = useRef<string | null>(null);
  // Store onChange in a ref to avoid re-renders when parent doesn't memoize it
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  // Check if all dependencies are filled
  const dependencyEntries = Object.entries(populateDependencies || {});
  const missingDependencies = dependencyEntries.filter(
    ([, formFieldOrConst]) => {
      // Skip checking for const values (objects with "const" key)
      if (typeof formFieldOrConst === 'object' && formFieldOrConst !== null && 'const' in formFieldOrConst) {
        return false;
      }
      // Skip nested objects (like filters) - they handle their own const values
      if (typeof formFieldOrConst === 'object' && formFieldOrConst !== null) {
        // Check if any nested form field references are missing
        for (const nestedValue of Object.values(formFieldOrConst)) {
          if (typeof nestedValue === 'object' && nestedValue !== null && 'const' in nestedValue) {
            continue; // Skip const values
          }
          const formField = nestedValue as string;
          if (!formValues[formField] && formValues[formField] !== 0) {
            return true; // Missing nested dependency
          }
        }
        return false;
      }
      // Check if form field is filled
      return !formValues[formFieldOrConst as string] && formValues[formFieldOrConst as string] !== 0;
    }
  );
  const hasMissingDependencies = missingDependencies.length > 0;

  // Build request data from dependencies
  const dependencyData: Record<string, any> = {};
  for (const [toolParam, formFieldOrConst] of dependencyEntries) {
    // Check if this is a const value (object with "const" key)
    if (typeof formFieldOrConst === 'object' && formFieldOrConst !== null && 'const' in formFieldOrConst) {
      // Use the const value directly
      dependencyData[toolParam] = (formFieldOrConst as { const: any }).const;
    } else if (typeof formFieldOrConst === 'object' && formFieldOrConst !== null) {
      // Nested object (e.g., filters: { org_id: "org_id", status: { const: "open" } })
      const nestedData: Record<string, any> = {};
      for (const [nestedKey, nestedValue] of Object.entries(formFieldOrConst)) {
        if (typeof nestedValue === 'object' && nestedValue !== null && 'const' in nestedValue) {
          // Nested const value
          nestedData[nestedKey] = (nestedValue as { const: any }).const;
        } else {
          // Nested form field reference
          const formField = nestedValue as string;
          if (formValues[formField] !== undefined && formValues[formField] !== '') {
            nestedData[nestedKey] = formValues[formField];
          }
        }
      }
      if (Object.keys(nestedData).length > 0) {
        dependencyData[toolParam] = nestedData;
      }
    } else {
      // Use form field value (existing behavior)
      const formField = formFieldOrConst as string;
      if (formValues[formField] !== undefined && formValues[formField] !== '') {
        dependencyData[toolParam] = formValues[formField];
      }
    }
  }
  // Create a stable string for useEffect dependency
  const dependencyDataKey = JSON.stringify(dependencyData);

  // Handle value change with type conversion
  const handleChange = (newValue: string | string[]) => {
    if (multiple) {
      // For multi-select, newValue is already an array of strings
      if (paramType === 'number') {
        // Convert string array to number array
        const numArray = (newValue as string[]).map(v => {
          const num = parseFloat(v);
          return isNaN(num) ? v : num;
        });
        onChange(numArray as number[]);
      } else {
        onChange(newValue as string[]);
      }
    } else {
      // Single value handling
      if (paramType === 'number' && newValue !== '') {
        const numValue = parseFloat(newValue as string);
        onChange(isNaN(numValue) ? newValue : numValue);
      } else {
        onChange(newValue as string);
      }
    }
  };

  // Clear options when populate configuration or dependencies change to avoid stale data
  // Skip clearing on initial mount to preserve any initial value
  useEffect(() => {
    if (isInitialMount.current) {
      isInitialMount.current = false;
      prevDependencyDataKey.current = dependencyDataKey;
      return;
    }

    // Only clear value if dependencies actually changed (not on initial mount)
    const depsChanged = prevDependencyDataKey.current !== dependencyDataKey;
    prevDependencyDataKey.current = dependencyDataKey;

    setOptions([]);
    setError(null);
    setManualMode(false);
    setLoading(true); // Reset to loading state when config changes

    // Clear the selected value when dependencies change to avoid stale/invalid selections
    if (depsChanged) {
      if (multiple) {
        onChangeRef.current([]);
      } else {
        onChangeRef.current('');
      }
    }
  }, [populateFrom, populateField, populateValue, populateDisplay, dependencyDataKey, multiple]);

  // Fetch options from the specified tool
  useEffect(() => {
    // Skip if dependencies are not met
    if (hasMissingDependencies) {
      setOptions([]);
      return;
    }

    // Skip if we already have options (unless this is a retry or dependencies changed)
    if (options.length > 0 && retryCount === 0) {
      return;
    }

    const abortController = new AbortController();
    let isMounted = true;

    const fetchOptions = async () => {
      // Find the tool to call for populating this field
      const populateTool = dataTypes.find((dt: DataType) =>
        getToolEndpoint(dt) === populateFrom
      );
      if (!populateTool) {
        if (isMounted) {
          setError(`Tool '${populateFrom}' not found`);
          setLoading(false);
          setManualMode(true); // Enable manual mode so error is visible
        }
        return;
      }

      if (isMounted) {
        setLoading(true);
        setError(null);
      }

      try {
        const res = await makeToolRequest({
          path: populateTool._internal.url,
          data: dependencyData,
          token,
          signal: abortController.signal,
        });

        if (!isMounted) return;

        const fieldData = res.data?.[populateField];
        // Ensure we have an array (handles non-array responses gracefully)
        const rawData = Array.isArray(fieldData) ? fieldData : [];

        // Handle both string arrays and object arrays
        let newOptions: { value: string; display: string }[] = [];

        // Check for object array: first element must be non-null object (typeof null === 'object')
        if (rawData.length > 0 && rawData[0] !== null && typeof rawData[0] === 'object') {
          // Object array: use populateValue and populateDisplay
          const valueKey = populateValue || 'value';
          const displayTemplate = populateDisplay || valueKey;

          // Helper to get display text - supports template syntax like "{first_name} {last_name}"
          const getDisplayText = (item: any): string => {
            if (displayTemplate.includes('{')) {
              // Template syntax: replace {field} with item.field
              return displayTemplate.replace(/\{(\w+)\}/g, (_, field) =>
                String(item[field] ?? '')
              ).trim();
            }
            // Simple field name
            return String(item[displayTemplate] ?? item[valueKey] ?? '');
          };

          newOptions = rawData
            .filter((item: any) => item !== null && item !== undefined)
            .map((item: any) => ({
              value: String(item[valueKey] ?? ''),
              display: getDisplayText(item),
            }));
        } else {
          // String array: values and displays are the same
          newOptions = rawData
            .filter((item: any) => item !== null && item !== undefined)
            .map((item: any) => ({
              value: String(item),
              display: String(item),
            }));
        }

        setOptions(newOptions);
      } catch (err: any) {
        if (!isMounted) return;
        if (isRequestCanceled(err)) return;
        console.error(`Failed to fetch options for ${paramName}:`, err);
        setError(err.message || 'Failed to fetch options');
        setManualMode(true); // Auto-switch to manual mode on error
      } finally {
        if (isMounted) setLoading(false);
      }
    };

    fetchOptions();

    return () => {
      isMounted = false;
      abortController.abort();
    };
  }, [populateFrom, populateField, populateValue, populateDisplay, paramName, token, retryCount, hasMissingDependencies, dependencyDataKey]);

  const baseInputClass = "w-full rounded-lg border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600 text-sm";

  // Retry fetching options
  const handleRetry = () => {
    setError(null);
    setManualMode(false);
    setRetryCount((c: number) => c + 1);
  };

  // Toggle button component
  const ModeToggleButton = () => (
    <button
      type="button"
      onClick={() => setManualMode(!manualMode)}
      className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-indigo-600 transition-colors"
      title={manualMode ? "Switch to dropdown" : "Enter manually"}
    >
      {manualMode ? <ListIcon /> : <PencilIcon />}
    </button>
  );

  // Retry button component (shown when there was an error)
  const RetryButton = () => (
    <button
      type="button"
      onClick={handleRetry}
      className="absolute right-9 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-indigo-600 transition-colors"
      title="Retry loading options"
    >
      <RefreshIcon />
    </button>
  );

  // Show disabled state when dependencies are not met
  if (hasMissingDependencies) {
    // Extract actual field names from missing dependencies (handles nested objects like filters)
    const missingFieldNames = missingDependencies.flatMap(([, formFieldOrConst]) => {
      if (typeof formFieldOrConst === 'object' && formFieldOrConst !== null && !('const' in formFieldOrConst)) {
        // Nested object - extract missing field names from it
        return Object.values(formFieldOrConst)
          .filter((v): v is string => typeof v === 'string')
          .filter(field => !formValues[field] && formValues[field] !== 0);
      }
      return typeof formFieldOrConst === 'string' ? [formFieldOrConst] : [];
    });
    const uniqueFieldNames = Array.from(new Set(missingFieldNames)).join(', ');
    return (
      <div className={`${baseInputClass} ${className} bg-gray-100 text-gray-400 cursor-not-allowed`}>
        Select {uniqueFieldNames} first
      </div>
    );
  }

  if (loading) {
    return (
      <div className={`${baseInputClass} ${className} bg-gray-50 text-gray-500 flex items-center`}>
        <svg className="animate-spin h-4 w-4 mr-2" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
        </svg>
        Loading options...
      </div>
    );
  }

  // Manual input mode (also shown automatically on error)
  if (manualMode) {
    const displayValue = multiple ? (Array.isArray(value) ? value.join(', ') : '') : (value ?? '');
    const handleManualChange = (inputValue: string) => {
      if (multiple) {
        // Split by comma and trim whitespace
        const values = inputValue.split(',').map(v => v.trim()).filter(v => v !== '');
        handleChange(values);
      } else {
        handleChange(inputValue);
      }
    };

    return (
      <div className="relative">
        <input
          type={multiple ? 'text' : (paramType === 'number' ? 'number' : 'text')}
          data-param-name={paramName}
          value={displayValue}
          onChange={(e) => handleManualChange(e.target.value)}
          className={`${baseInputClass} ${className} ${error ? 'pr-16 border-orange-300' : 'pr-10'}`}
          placeholder={error ? `Enter manually (${error})` : (placeholder || (multiple ? 'Enter values separated by commas' : 'Enter value manually'))}
          required={required}
        />
        {error && <RetryButton />}
        <ModeToggleButton />
      </div>
    );
  }

  // Empty state - no options found after loading
  if (options.length === 0) {
    return (
      <div className="relative">
        <div className={`${baseInputClass} ${className} pr-16 bg-gray-50 text-gray-500`}>
          No options found
        </div>
        <RetryButton />
        <ModeToggleButton />
      </div>
    );
  }

  // Multi-select: Checkbox mode
  if (multiple) {
    const selectedValues = Array.isArray(value) ? value.map(String) : [];

    const handleCheckboxChange = (optionValue: string, checked: boolean) => {
      let newValues: string[];
      if (checked) {
        newValues = [...selectedValues, optionValue];
      } else {
        newValues = selectedValues.filter(v => v !== optionValue);
      }
      handleChange(newValues);
    };

    return (
      <div className={`${className}`}>
        <div className="text-sm text-gray-700 mb-2">
          {placeholder || 'Select options'}
          {required && <span className="text-red-500 ml-1">*</span>}
        </div>
        <div className="space-y-2 max-h-64 overflow-y-auto border border-gray-300 rounded-lg p-3 bg-white">
          {options.map((option) => {
            const isChecked = selectedValues.includes(option.value);
            return (
              <label
                key={option.value}
                className="flex items-center space-x-2 cursor-pointer hover:bg-gray-50 p-1 rounded"
              >
                <input
                  type="checkbox"
                  data-param-name={paramName}
                  value={option.value}
                  checked={isChecked}
                  onChange={(e) => handleCheckboxChange(option.value, e.target.checked)}
                  className="h-4 w-4 text-indigo-600 border-gray-300 rounded focus:ring-indigo-600"
                />
                <span className="text-sm text-gray-900">{option.display}</span>
              </label>
            );
          })}
        </div>
        {selectedValues.length > 0 && (
          <div className="text-xs text-gray-500 mt-1">
            {selectedValues.length} selected
          </div>
        )}
      </div>
    );
  }

  // Single-select: Dropdown mode
  return (
    <div className="relative">
      <select
        data-param-name={paramName}
        value={value ?? ''}
        onChange={(e) => handleChange(e.target.value)}
        className={`${baseInputClass} ${className} pr-10`}
        required={required}
      >
        <option value="">{placeholder || 'Select an option'}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.display}
          </option>
        ))}
      </select>
      <ModeToggleButton />
    </div>
  );
}
