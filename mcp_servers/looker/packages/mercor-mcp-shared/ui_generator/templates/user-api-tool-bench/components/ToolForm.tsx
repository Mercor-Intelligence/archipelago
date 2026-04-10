// Reusable tool form component - renders parameter inputs, executes tool, and shows response.
// Used by both ApiTool (inline) and ToolDialog (modal).
import { useState, useEffect } from 'react';
import { DataType, getToolEndpoint, AuthUser, personas } from '@/lib/api-config';
import { makeToolRequest } from '@mcp-shared/utils/api';
import { ListInput, ObjectInput, ObjectListInput, buildDefaultObject } from '@mcp-shared/ui/inputs';
import ExecuteButton from '@mcp-shared/ui/ExecuteButton';
import ErrorDisplay from '@mcp-shared/ui/ErrorDisplay';
import ToolHeader from '@mcp-shared/ui/ToolHeader';
import PersonasTable from '@mcp-shared/ui/PersonasTable';
import ResponseDisplay from '@mcp-shared/ui/ResponseDisplay';
import CsvInput from '@mcp-shared/ui/CsvInput';
import FileBase64Input from '@mcp-shared/ui/FileBase64Input';
import ParameterLabel from '@mcp-shared/ui/ParameterLabel';
import ToolParametersHeader from '@mcp-shared/ui/ToolParametersHeader';
import { resolveParamFields } from '@mcp-shared/utils/paramFields';
import DynamicSelect from '@mcp-shared/ui/DynamicSelect';

export interface ToolFormProps {
  dataType: DataType;
  /** @deprecated Use setAuthToken() in api.ts instead. Kept for backwards compatibility. */
  token?: string;
  onLogin?: (token: string, user: AuthUser) => void;
  /** @deprecated Auth errors are now handled by api.ts via setOnAuthError(). */
  onLogout?: () => void;
}

export default function ToolForm({ dataType, onLogin }: ToolFormProps) {
  const [parameters, setParameters] = useState<Record<string, any>>({});
  const [response, setResponse] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingPage, setLoadingPage] = useState(false);
  const [csvInputMode, setCsvInputMode] = useState<Record<string, 'upload' | 'paste'>>({});

  // Initialize parameter defaults when dataType changes
  useEffect(() => {
    if (dataType && dataType.parameters) {
      const defaults: Record<string, any> = {};
      dataType.parameters.forEach(param => {
        if (param.default !== undefined) {
          defaults[param.name] = param.default;
        } else if (param.isList) {
          if (param.required) {
            const fields = resolveParamFields(param);
            if (param.type === 'object' && fields) {
              const emptyItem: Record<string, any> = {};
              fields.forEach((field: any) => {
                if (field.default !== undefined) {
                  emptyItem[field.name] = field.default;
                } else if (field.type === 'boolean') {
                  emptyItem[field.name] = false;
                } else if (field.type === 'number') {
                  emptyItem[field.name] = 0;
                } else {
                  emptyItem[field.name] = '';
                }
              });
              defaults[param.name] = [emptyItem];
            } else {
              const defaultValue = param.type === 'number' ? 0 : param.type === 'boolean' ? false : param.type === 'object' ? {} : '';
              defaults[param.name] = [defaultValue];
            }
          } else {
            defaults[param.name] = [];
          }
        } else if (param.type === 'object' && resolveParamFields(param)) {
          defaults[param.name] = param.required ? {} : null;
        }
      });
      setParameters(defaults);
    }
    setResponse(null);
    setError(null);
  }, [dataType]);

  const handleFetchData = async () => {
    setLoading(true);
    setError(null);
    setResponse(null);

    try {
      const cleanedParameters: Record<string, any> = {};

      dataType.parameters?.forEach(param => {
        const value = parameters[param.name];

        if (value !== '' && value !== null && value !== undefined) {
          let parsedValue = value;

          if (param.isList) {
            if (Array.isArray(value)) {
              parsedValue = value;
            } else if (typeof value === 'string') {
              try {
                parsedValue = JSON.parse(value);
              } catch (e) {
                throw new Error(`Invalid JSON in field "${param.label}": ${e instanceof Error ? e.message : 'Parse error'}`);
              }
            }
          } else if (param.type === 'object' && typeof value === 'string') {
            try {
              parsedValue = JSON.parse(value);
            } catch (e) {
              throw new Error(`Invalid JSON in field "${param.label}": ${e instanceof Error ? e.message : 'Parse error'}`);
            }
          }

          cleanedParameters[param.name] = parsedValue;
        } else if (param.default !== undefined) {
          cleanedParameters[param.name] = param.default;
        } else if (param.required) {
          cleanedParameters[param.name] = value;
        }
      });

      const toolName = getToolEndpoint(dataType);

      try {
        const res = await makeToolRequest({
          toolName,
          method: dataType._internal.method.toUpperCase(),
          data: cleanedParameters,
        });
        setResponse({ success: true, data: res.data });

        if (toolName === 'login_tool' && res.data && res.data.token && res.data.user) {
          onLogin?.(res.data.token, res.data.user);
        }
      } catch (err: any) {
        setError(err.message || 'An unexpected error occurred');
      }
    } catch (err: any) {
      // Parameter validation errors (JSON parse failures)
      const errorStr = err.message || 'An unexpected error occurred';
      setError(errorStr);
    } finally {
      setLoading(false);
    }
  };

  const handleLoadPage = async (page: number) => {
    setLoadingPage(true);
    setError(null);
    try {
      const pageParam = response?.data?._pagination?.page_param || 'page_number';
      const pageParams = { ...parameters, [pageParam]: page };

      setParameters(prev => ({ ...prev, [pageParam]: page }));

      const toolName = getToolEndpoint(dataType);
      const res = await makeToolRequest({
        toolName,
        method: dataType._internal.method.toUpperCase(),
        data: pageParams,
      });
      setResponse({ success: true, data: res.data });
      setError(null);
    } catch (err: any) {
      setError(err.message || 'Failed to load page');
    } finally {
      setLoadingPage(false);
    }
  };

  const handleParameterChange = (paramName: string, value: any) => {
    setParameters(prev => ({
      ...prev,
      [paramName]: value,
    }));
  };

  const renderParameterInput = (param: any) => {
    const value = parameters[param.name] ?? '';
    const baseInputClass = "w-full rounded-lg border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600 text-sm";

    if (param.populateFrom && param.populateField) {
      return (
        <DynamicSelect
          value={value}
          onChange={(v: string | number | string[] | number[]) => handleParameterChange(param.name, v)}
          populateFrom={param.populateFrom}
          populateField={param.populateField}
          populateValue={param.populateValue}
          populateDisplay={param.populateDisplay}
          populateDependencies={param.populateDependencies}
          formValues={parameters}
          paramName={param.name}
          paramType={param.type}
          required={param.required}
          placeholder={param.placeholder || `Select ${param.label}`}
          multiple={param.isList}
        />
      );
    }

    if (param.isList && param.enum) {
      const selectedValues = Array.isArray(value) ? value : [];
      return (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            {param.enum.map((option: string) => {
              const isSelected = selectedValues.includes(option);
              const description = param.enumDescriptions?.[option];
              const displayText = description ? `${option} - ${description}` : option;
              return (
                <label
                  key={option}
                  className={`inline-flex items-center px-3 py-1.5 rounded-lg border cursor-pointer transition-colors ${
                    isSelected
                      ? 'bg-indigo-100 border-indigo-500 text-indigo-700'
                      : 'bg-white border-gray-300 text-gray-700 hover:border-gray-400'
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={(e) => {
                      const newValues = e.target.checked
                        ? [...selectedValues, option]
                        : selectedValues.filter((v: string) => v !== option);
                      handleParameterChange(param.name, newValues);
                    }}
                    className="sr-only"
                    data-param-name={param.name}
                  />
                  <span className="text-sm">{displayText}</span>
                </label>
              );
            })}
          </div>
          {selectedValues.length > 0 && (
            <p className="text-xs text-gray-500">
              Selected: {selectedValues.join(', ')}
            </p>
          )}
        </div>
      );
    }

    if (param.isList) {
      const fields = resolveParamFields(param);
      if (param.type === 'object' && fields) {
        const listValue = Array.isArray(value) ? value : [];
        return (
          <ObjectListInput
            fields={fields}
            items={listValue}
            onChange={(items) => handleParameterChange(param.name, items)}
            paramName={param.name}
            required={param.required}
          />
        );
      }

      if (param.type === 'object' || param.isJsonField) {
        return (
          <div>
            <textarea
              data-param-name={param.name}
              value={typeof value === 'string' ? value : JSON.stringify(value || [], null, 2)}
              onChange={(e) => handleParameterChange(param.name, e.target.value)}
              className={`${baseInputClass} font-mono`}
              placeholder={param.jsonExample || '[\n  { "key": "value" }\n]'}
              rows={6}
              required={param.required}
            />
            <p className="mt-1 text-xs text-gray-500">Enter a JSON array of objects</p>
          </div>
        );
      }

      const listValue = Array.isArray(value) ? value : [];
      return (
        <ListInput
          items={listValue}
          itemType={param.type}
          onChange={(items) => handleParameterChange(param.name, items)}
          placeholder={param.placeholder}
          min={param.min}
          max={param.max}
          paramName={param.name}
          required={param.required}
        />
      );
    }

    if (param.enum) {
      return (
        <select
          data-param-name={param.name}
          value={value}
          onChange={(e) => handleParameterChange(param.name, e.target.value)}
          className={baseInputClass}
          required={param.required}
        >
          <option value="">Select {param.label}</option>
          {param.enum.map((option: string) => {
            const description = param.enumDescriptions?.[option];
            const displayText = description ? `${option} - ${description}` : option;
            return (
              <option key={option} value={option}>{displayText}</option>
            );
          })}
        </select>
      );
    }

    if (param.type === 'boolean') {
      return (
        <div className="flex items-center">
          <input
            type="checkbox"
            data-param-name={param.name}
            checked={value || false}
            onChange={(e) => handleParameterChange(param.name, e.target.checked)}
            className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
          />
          <span className="ml-2 text-sm text-gray-500">Enable this option</span>
        </div>
      );
    }

    if (param.type === 'date') {
      return (
        <input
          type="date"
          data-param-name={param.name}
          value={value}
          onChange={(e) => handleParameterChange(param.name, e.target.value)}
          className={baseInputClass}
          required={param.required}
        />
      );
    }

    if (param.type === 'number') {
      return (
        <input
          type="number"
          data-param-name={param.name}
          value={value}
          onChange={(e) => handleParameterChange(param.name, parseFloat(e.target.value) || 0)}
          className={baseInputClass}
          placeholder={param.placeholder}
          required={param.required}
          min={param.min}
          max={param.max}
        />
      );
    }

    const isBase64FileParam = (
      param.name.toLowerCase().includes('file_content_base64') ||
      (param.name.toLowerCase().includes('base64') && param.name.toLowerCase().includes('file')) ||
      (param.name.toLowerCase().includes('content') && param.name.toLowerCase().includes('base64'))
    );

    if (isBase64FileParam) {
      let accept: string | undefined;
      const descLower = (param.description || '').toLowerCase();
      if (descLower.includes('.twb') || descLower.includes('tableau workbook')) {
        accept = '.twb,.twbx';
      } else if (descLower.includes('image') || descLower.includes('.png') || descLower.includes('.jpg')) {
        accept = 'image/*';
      } else if (descLower.includes('.pdf')) {
        accept = '.pdf';
      }

      return (
        <FileBase64Input
          value={value}
          onChange={(base64) => handleParameterChange(param.name, base64)}
          onFileNameChange={(fileName) => {
            if (dataType?.parameters?.some(p => p.name === 'file_name')) {
              handleParameterChange('file_name', fileName);
            }
          }}
          paramName={param.name}
          required={param.required}
          accept={accept}
          description={param.description}
        />
      );
    }

    if (param.name.toLowerCase().includes('csv_content') || param.name.toLowerCase().includes('csv')) {
      const mode = csvInputMode[param.name] || 'upload';

      return (
        <CsvInput
          value={value}
          onChange={(v) => handleParameterChange(param.name, v)}
          paramName={param.name}
          required={param.required}
          mode={mode}
          onModeChange={(m) => setCsvInputMode(prev => ({ ...prev, [param.name]: m }))}
        />
      );
    }

    const objectFields = resolveParamFields(param);
    if (param.type === 'object' && objectFields) {
      const objectValue = typeof value === 'object' && value !== null ? value : null;

      if (!param.required) {
        if (!objectValue) {
          return (
            <button
              type="button"
              onClick={() => {
                handleParameterChange(param.name, buildDefaultObject(objectFields));
              }}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-indigo-700 bg-indigo-50 border border-indigo-200 rounded-md hover:bg-indigo-100 transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
              Set {param.label}
            </button>
          );
        }
        return (
          <div>
            <button
              type="button"
              onClick={() => handleParameterChange(param.name, null)}
              className="mb-2 flex items-center gap-1 px-2 py-1 text-xs font-medium text-red-600 bg-red-50 border border-red-200 rounded hover:bg-red-100 transition-colors"
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
              Remove {param.label}
            </button>
            <ObjectInput
              fields={objectFields}
              value={objectValue}
              onChange={(v) => handleParameterChange(param.name, v)}
              paramName={param.name}
            />
          </div>
        );
      }

      return (
        <ObjectInput
          fields={objectFields}
          value={objectValue || {}}
          onChange={(v) => handleParameterChange(param.name, v)}
          paramName={param.name}
        />
      );
    }

    if (param.type === 'object' || param.isJsonField) {
      return (
        <div>
          <textarea
            data-param-name={param.name}
            value={typeof value === 'string' ? value : JSON.stringify(value || {}, null, 2)}
            onChange={(e) => handleParameterChange(param.name, e.target.value)}
            className={`${baseInputClass} font-mono`}
            placeholder={param.jsonExample || '{\n  "key": "value"\n}'}
            rows={6}
            required={param.required}
          />
          <p className="mt-1 text-xs text-gray-500">Enter JSON object</p>
        </div>
      );
    }

    return (
      <div>
        <input
          type="text"
          data-param-name={param.name}
          value={value}
          onChange={(e) => handleParameterChange(param.name, e.target.value)}
          className={baseInputClass}
          placeholder={param.placeholder}
          required={param.required}
          minLength={param.minLength}
          maxLength={param.maxLength}
          pattern={param.pattern}
        />
      </div>
    );
  };

  return (
    <div data-screenshot="tool-panel" className="rounded-lg border border-gray-300 p-6 shadow-sm bg-white h-fit">
      <ToolHeader dataType={dataType} />

      {getToolEndpoint(dataType) === 'login_tool' && (
        <PersonasTable personas={personas} />
      )}

      {dataType.parameters && dataType.parameters.length > 0 && (
        <div className="space-y-4 mb-6">
          <h3 className="text-sm font-semibold text-gray-900 uppercase tracking-wide">Parameters</h3>
          <ToolParametersHeader dataType={dataType} />
          {dataType.parameters.map(param => (
            <div key={param.name} className="space-y-2">
              <ParameterLabel
                label={param.label}
                required={param.required}
                description={param.description}
                param={param}
              />
              {renderParameterInput(param)}
            </div>
          ))}
        </div>
      )}

      <ExecuteButton onClick={handleFetchData} loading={loading} />

      {error && <ErrorDisplay error={error} />}

      {response && <ResponseDisplay response={response} onLoadPage={handleLoadPage} loadingPage={loadingPage} />}
    </div>
  );
}
