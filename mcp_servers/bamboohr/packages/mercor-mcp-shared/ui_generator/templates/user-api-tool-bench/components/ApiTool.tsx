// AUTO-GENERATED - Generic MCP UI Component
import { useState, useEffect } from 'react';
import axios from 'axios';
import { DataType, dataTypes, models, getToolEndpoint, AuthUser, personas } from '@/lib/api-config';
import DocsViewer, { useDocsAvailable } from '@mcp-shared/DocsViewer';
import { getBasePath, getApiBase } from '@mcp-shared/utils/api';
import SearchBar from '@mcp-shared/ui/SearchBar';
import { ListInput, ObjectInput, ObjectListInput } from '@mcp-shared/ui/inputs';
import ExecuteButton from '@mcp-shared/ui/ExecuteButton';
import ErrorDisplay from '@mcp-shared/ui/ErrorDisplay';
import ToolHeader from '@mcp-shared/ui/ToolHeader';
import PersonasTable from '@mcp-shared/ui/PersonasTable';
import EmptyState from '@mcp-shared/ui/EmptyState';
import ResponseDisplay from '@mcp-shared/ui/ResponseDisplay';
import CsvInput from '@mcp-shared/ui/CsvInput';
import FileBase64Input from '@mcp-shared/ui/FileBase64Input';
import ToolsSidebar from '@mcp-shared/ui/ToolsSidebar';
import ModelCard from '@mcp-shared/ui/ModelCard';
import ModelDetail from '@mcp-shared/ui/ModelDetail';
import ParameterLabel from '@mcp-shared/ui/ParameterLabel';
import Header from '@mcp-shared/Header';
import { resolveParamFields } from '@mcp-shared/utils/paramFields';
import { useTrajectoryOptional } from '@mcp-shared-lib/TrajectoryContext';
import DynamicSelect from '@mcp-shared/ui/DynamicSelect';

export default function ApiTool({
  token,
  user,
  onLogout,
  onLogin,
}: {
  token: string;
  user: AuthUser | null;
  onLogout: () => void;
  onLogin: (token: string, user: AuthUser) => void;
}) {
  // Trajectory context for recording tool calls
  const trajectory = useTrajectoryOptional();
  const [currentTab, setCurrentTab] = useState<'tools' | 'models' | 'docs'>('tools');
  const docsAvailable = useDocsAvailable(getBasePath());
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [modelSearchQuery, setModelSearchQuery] = useState('');
  const [selectedDataType, setSelectedDataType] = useState<DataType | null>(null);
  const [parameters, setParameters] = useState<Record<string, any>>({});
  const [response, setResponse] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set());
  const [csvInputMode, setCsvInputMode] = useState<Record<string, 'upload' | 'paste'>>({});

  // Listen for API_BASE from parent window via postMessage
  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      if (event.data.type === 'SET_API_BASE') {
        console.log('[Iframe] Received API_BASE via postMessage:', event.data.apiBase);
        (window as any).__API_BASE__ = event.data.apiBase;
      }
    };

    window.addEventListener('message', handleMessage);

    // Also check URL params for api_base
    const urlParams = new URLSearchParams(window.location.search);
    const apiBaseParam = urlParams.get('api_base');
    if (apiBaseParam) {
      console.log('[Iframe] Using API_BASE from URL param:', apiBaseParam);
      (window as any).__API_BASE__ = apiBaseParam;
    }

    return () => window.removeEventListener('message', handleMessage);
  }, []);

  // Use static tools from api-config.ts (no runtime discovery)
  const dataTypesByCategory = dataTypes.reduce((acc, dt) => {
    if (!acc[dt.category]) {
      acc[dt.category] = [];
    }
    acc[dt.category].push(dt);
    return acc;
  }, {} as Record<string, DataType[]>);

  const categories = Object.keys(dataTypesByCategory);

  // Filter categories and tools based on search query
  const filteredCategories = categories.filter(cat => {
    const tools = dataTypesByCategory[cat];
    if (tools.length === 0) return false;
    if (!searchQuery) return true;

    // Show category if it matches or any of its tools match
    const categoryMatches = cat.toLowerCase().includes(searchQuery.toLowerCase());
    const toolMatches = tools.some(dt =>
      dt.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      dt.description.toLowerCase().includes(searchQuery.toLowerCase())
    );
    return categoryMatches || toolMatches;
  });

  // Also filter tools within each category
  const filteredDataTypes = selectedDataType ?
    [] :
    dataTypes.filter(dt => {
      // Apply search filter
      return dt.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        dt.description.toLowerCase().includes(searchQuery.toLowerCase());
    });

  useEffect(() => {
    if (selectedDataType && selectedDataType.parameters) {
      const defaults: Record<string, any> = {};
      selectedDataType.parameters.forEach(param => {
        if (param.default !== undefined) {
          defaults[param.name] = param.default;
        } else if (param.isList) {
          // For required lists, pre-create one empty item
          if (param.required) {
            const fields = resolveParamFields(param);
            if (param.type === 'object' && fields) {
              // Create empty object with default values for each field
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
              // Primitive list - start with type-appropriate default
              const defaultValue = param.type === 'number' ? 0 : param.type === 'boolean' ? false : param.type === 'object' ? {} : '';
              defaults[param.name] = [defaultValue];
            }
          } else {
            // Optional lists start empty
            defaults[param.name] = [];
          }
        } else if (param.type === 'object' && resolveParamFields(param)) {
          // Initialize object parameters with field definitions to empty objects
          defaults[param.name] = {};
        }
      });
      setParameters(defaults);
    }
  }, [selectedDataType]);

  const handleFetchData = async () => {
    if (!selectedDataType) {
      return;
    }

    setLoading(true);
    setError(null);
    setResponse(null);

    try {
      // Use default values for empty optional parameters
      const cleanedParameters: Record<string, any> = {};

      selectedDataType.parameters?.forEach(param => {
        const value = parameters[param.name];

        if (value !== '' && value !== null && value !== undefined) {
          // User provided a value
          let parsedValue = value;

          // Handle list parameters
          if (param.isList) {
            if (Array.isArray(value)) {
              // Already an array (from ListInput component) - use as-is
              parsedValue = value;
            } else if (typeof value === 'string') {
              // JSON string (from object list textarea) - parse it
              try {
                parsedValue = JSON.parse(value);
              } catch (e) {
                throw new Error(`Invalid JSON in field "${param.label}": ${e instanceof Error ? e.message : 'Parse error'}`);
              }
            }
          } else if (param.type === 'object' && typeof value === 'string') {
            // Parse JSON strings for non-list object types
            try {
              parsedValue = JSON.parse(value);
            } catch (e) {
              throw new Error(`Invalid JSON in field "${param.label}": ${e instanceof Error ? e.message : 'Parse error'}`);
            }
          }

          cleanedParameters[param.name] = parsedValue;
        } else if (param.default !== undefined) {
          // Use default value for optional parameters
          cleanedParameters[param.name] = param.default;
        } else if (param.required) {
          // Required parameter with no value (will cause validation error)
          cleanedParameters[param.name] = value;
        }
        // Optional parameters without defaults and no user value are omitted
      });

      // Call tools directly (both dynamic and static)
      const method = selectedDataType._internal.method.toUpperCase();
      const apiBase = getApiBase();
      let url = `${apiBase}${selectedDataType._internal.url}`;

      // Build query parameters and handle path parameters
      const queryParams: Record<string, any> = {};
      const bodyParams: Record<string, any> = {};

      if (selectedDataType.parameters) {
        selectedDataType.parameters.forEach((param) => {
          const value = cleanedParameters[param.name];

          if (value !== undefined && value !== null && value !== '') {
            // Check if parameter is in URL path (e.g., {accountId})
            const urlTemplate = `{${param.name}}`;
            if (url.includes(urlTemplate)) {
              url = url.replace(urlTemplate, encodeURIComponent(String(value)));
            } else if (param.location === 'query') {
              queryParams[param.name] = value;
            } else if (param.location === 'body') {
              bodyParams[param.name] = value;
            }
          }
        });
      }

      // Build request config
      const requestConfig: any = {
        method,
        url,
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
      };

      // Add authentication token to all requests if available
      // (Protected tools will use it, public tools will ignore it)
      if (token) {
        requestConfig.headers.Authorization = `Bearer ${token}`;
      }

      // Add query parameters
      if (Object.keys(queryParams).length > 0) {
        requestConfig.params = queryParams;
      }

      // Add trajectory session parameter if recording is active
      if (trajectory?.sessionId) {
        requestConfig.params = {
          ...requestConfig.params,
          trajectory_session: trajectory.sessionId,
        };
      }

      // Add body parameters
      if (Object.keys(bodyParams).length > 0) {
        requestConfig.data = bodyParams;
      } else if (method === 'POST' || method === 'PUT' || method === 'PATCH') {
        // For POST/PUT/PATCH, send all cleaned parameters as body if no specific body params
        requestConfig.data = cleanedParameters;
      }

      // Make the request
      const res = await axios(requestConfig);
      setResponse({ success: true, data: res.data });

      // Check if this was a login_tool call and handle authentication
      const toolName = getToolEndpoint(selectedDataType);
      if (toolName === 'login_tool' && res.data && res.data.token && res.data.user) {
        // Successfully logged in - store the token and user info
        onLogin(res.data.token, res.data.user);
      }
    } catch (err: any) {
      // Handle error response properly
      const errorMessage = err.response?.data?.detail
        || err.response?.data?.error
        || err.message
        || 'An unexpected error occurred';
      const errorStr = typeof errorMessage === 'string' ? errorMessage : JSON.stringify(errorMessage);
      setError(errorStr);

      // Check for auth errors (expired token) and reset auth state
      const isAuthError = err.response?.status === 401
        || errorStr.toLowerCase().includes('expired')
        || errorStr.toLowerCase().includes('invalid token')
        || errorStr.toLowerCase().includes('authentication required');
      if (isAuthError && token) {
        onLogout();
      }
    } finally {
      setLoading(false);
    }
  };

  const handleParameterChange = (paramName: string, value: any) => {
    setParameters(prev => ({
      ...prev,
      [paramName]: value,
    }));
  };

  const handleToggleCategory = (category: string) => {
    const newExpanded = new Set(expandedCategories);
    if (expandedCategories.has(category)) {
      newExpanded.delete(category);
    } else {
      newExpanded.add(category);
    }
    setExpandedCategories(newExpanded);
  };

  const handleSelectTool = (dataType: DataType | null) => {
    if (dataType && selectedDataType && selectedDataType.id === dataType.id) {
      setSelectedDataType(null);
      setParameters({});
      setResponse(null);
      setError('');
    } else {
      setSelectedDataType(dataType);
      setParameters({});
      setResponse(null);
      setError('');
    }
  };

  const renderParameterInput = (param: any) => {
    const value = parameters[param.name] ?? '';
    const baseInputClass = "w-full rounded-lg border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600 text-sm";

    // Dynamic select - populated from another tool's response
    // This overrides other field type handling when populateFrom is present
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
          token={token}
          required={param.required}
          placeholder={param.placeholder || `Select ${param.label}`}
          multiple={param.isList}
        />
      );
    }

    // Handle list of enum values as multi-select checkboxes
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

    // Handle list parameters with the ListInput component
    if (param.isList) {
      // For object lists with field definitions, use ObjectListInput
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

      // For object lists without field definitions, fall back to JSON textarea
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

      // For primitive types (string, number, boolean, date), use the ListInput component
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
            // Show description if available (e.g., "101 - Orders by Status")
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

    // Special handling for base64 file content (e.g., file_content_base64, workbook uploads)
    // Detects parameters that expect base64-encoded file data
    const isBase64FileParam = (
      param.name.toLowerCase().includes('file_content_base64') ||
      (param.name.toLowerCase().includes('base64') && param.name.toLowerCase().includes('file')) ||
      (param.name.toLowerCase().includes('content') && param.name.toLowerCase().includes('base64'))
    );

    if (isBase64FileParam) {
      // Determine file type filter from param description or name
      let accept: string | undefined;
      const descLower = (param.description || '').toLowerCase();
      if (descLower.includes('.twb') || descLower.includes('tableau workbook')) {
        accept = '.twb,.twbx';
      } else if (descLower.includes('image') || descLower.includes('.png') || descLower.includes('.jpg')) {
        accept = 'image/*';
      } else if (descLower.includes('.pdf')) {
        accept = '.pdf';
      }
      // Otherwise leave undefined to accept all files

      return (
        <FileBase64Input
          value={value}
          onChange={(base64) => handleParameterChange(param.name, base64)}
          onFileNameChange={(fileName) => {
            // Auto-populate file_name parameter if it exists
            if (selectedDataType?.parameters?.some(p => p.name === 'file_name')) {
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

    // Special handling for CSV content - use CsvInput component
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

    // Handle object type with field definitions - render sub-fields
    const objectFields = resolveParamFields(param);
    if (param.type === 'object' && objectFields) {
      const objectValue = typeof value === 'object' && value !== null ? value : {};
      return (
        <ObjectInput
          fields={objectFields}
          value={objectValue}
          onChange={(v) => handleParameterChange(param.name, v)}
          paramName={param.name}
        />
      );
    }

    // Handle object/JSON type without field definitions - fall back to textarea
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

  // Filter models based on search
  const filteredModels = models.filter(m =>
    m.name.toLowerCase().includes(modelSearchQuery.toLowerCase()) ||
    (m.docstring?.toLowerCase().includes(modelSearchQuery.toLowerCase()) ?? false)
  );

  return (
    <div className="min-h-screen bg-gradient-to-br from-indigo-50 via-white to-purple-50">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Header - can be overridden via components/overrides/Header.tsx */}
        <Header
          user={user}
          onLogout={onLogout}
          dataTypes={dataTypes}
          onLoginClick={() => {
            const loginTool = dataTypes.find(dt =>
              getToolEndpoint(dt) === 'login_tool'
            );
            if (loginTool) {
              setSelectedDataType(loginTool);
              setCurrentTab('tools');
            }
          }}
          onSelectTool={(tool) => {
            setSelectedDataType(tool);
            setCurrentTab('tools');
          }}
        />

        {/* Tab Navigation */}
        {(models.length > 0 || docsAvailable) && (
          <div className="mb-6 border-b border-gray-200">
            <nav className="-mb-px flex space-x-8">
              <button
                onClick={() => setCurrentTab('tools')}
                className={`py-4 px-1 border-b-2 font-medium text-sm transition-colors ${
                  currentTab === 'tools'
                    ? 'border-indigo-500 text-indigo-600'
                    : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                }`}
              >
                Tools
              </button>
              {models.length > 0 && (
                <button
                  onClick={() => setCurrentTab('models')}
                  className={`py-4 px-1 border-b-2 font-medium text-sm transition-colors ${
                    currentTab === 'models'
                      ? 'border-indigo-500 text-indigo-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                  }`}
                >
                  Models
                  <span className="ml-2 px-2 py-0.5 text-xs bg-indigo-100 text-indigo-700 rounded-full">
                    {models.length}
                  </span>
                </button>
              )}
              {docsAvailable && (
                <button
                  onClick={() => setCurrentTab('docs')}
                  className={`py-4 px-1 border-b-2 font-medium text-sm transition-colors ${
                    currentTab === 'docs'
                      ? 'border-indigo-500 text-indigo-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
                  }`}
                >
                  Docs
                </button>
              )}
            </nav>
          </div>
        )}

        {currentTab === 'tools' && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Sidebar - Collapsible Categories with Tools */}
          <ToolsSidebar
            searchQuery={searchQuery}
            onSearchChange={setSearchQuery}
            filteredCount={filteredDataTypes.length}
            categories={filteredCategories}
            dataTypesByCategory={dataTypesByCategory}
            expandedCategories={expandedCategories}
            onToggleCategory={handleToggleCategory}
            selectedDataType={selectedDataType}
            onSelectTool={handleSelectTool}
          />

          {/* Main Content */}
          <div className="lg:col-span-2 flex flex-col min-h-0">
            {!selectedDataType ? (
              <EmptyState
                title="Select a tool to get started"
                description="Choose a tool from the sidebar to configure and execute"
              />
            ) : (
              <div className="space-y-6 flex-1 overflow-y-auto">
                <div data-screenshot="tool-panel" className="rounded-lg border border-gray-300 p-6 shadow-sm bg-white h-fit">
                  {/* Header */}
                  <ToolHeader dataType={selectedDataType} />

                  {/* Personas Table for Login Tool */}
                  {getToolEndpoint(selectedDataType) === 'login_tool' && (
                    <PersonasTable personas={personas} />
                  )}

                  {/* Parameters Form */}
                  {selectedDataType.parameters && selectedDataType.parameters.length > 0 && (
                    <div className="space-y-4 mb-6">
                      <h3 className="text-sm font-semibold text-gray-900 uppercase tracking-wide">Parameters</h3>
                      {selectedDataType.parameters.map(param => (
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

                  {/* Execute Button */}
                  <ExecuteButton onClick={handleFetchData} loading={loading} />

                  {/* Error Display */}
                  {error && <ErrorDisplay error={error} />}

                  {/* Response Display */}
                  {response && <ResponseDisplay response={response} />}
                </div>
              </div>
            )}
          </div>
        </div>
        )}

        {/* Models Tab */}
        {currentTab === 'models' && (
          <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
            {/* Sidebar - Model List */}
            <div className="lg:col-span-1">
              <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 sticky top-4">
                <div className="mb-4">
                  <SearchBar
                    value={modelSearchQuery}
                    onChange={setModelSearchQuery}
                    placeholder="Search models..."
                  />
                </div>
                <h2 className="text-sm font-semibold mb-3 text-gray-700 uppercase tracking-wide">
                  Models ({filteredModels.length})
                </h2>
                <div className="space-y-1 max-h-[calc(100vh-16rem)] overflow-y-auto">
                  {filteredModels.map((model) => (
                    <button
                      key={model.name}
                      onClick={() => setSelectedModel(selectedModel === model.name ? null : model.name)}
                      className={`w-full text-left px-3 py-2 rounded-md transition-colors text-sm ${
                        selectedModel === model.name
                          ? 'bg-indigo-50 text-indigo-900 font-medium border border-indigo-200'
                          : 'hover:bg-gray-50 text-gray-700 border border-transparent'
                      }`}
                    >
                      <div className="flex items-center justify-between">
                        <span className="truncate">{model.name}</span>
                        {model.is_enum && (
                          <span className="ml-2 px-2 py-0.5 text-xs bg-purple-100 text-purple-700 rounded">
                            Enum
                          </span>
                        )}
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {/* Main Content */}
            <div className="lg:col-span-3">
              {selectedModel ? (
                (() => {
                  const model = models.find(m => m.name === selectedModel);
                  if (!model) return null;
                  return <ModelDetail model={model} />;
                })()
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  {filteredModels.map((model) => (
                    <ModelCard
                      key={model.name}
                      model={model}
                      onClick={() => setSelectedModel(model.name)}
                    />
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        {/* Docs Tab */}
        {currentTab === 'docs' && (
          <DocsViewer basePath={getBasePath()} />
        )}
      </div>
    </div>
  );
}
