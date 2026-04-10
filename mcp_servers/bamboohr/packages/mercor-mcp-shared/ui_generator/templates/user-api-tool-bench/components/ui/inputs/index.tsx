// Input components for list and object parameters
// These components have mutual dependencies, so they are co-located in this file.

import { resolveParamFields } from '../../utils/paramFields';

interface ListInputProps {
  items: any[];
  itemType: 'string' | 'number' | 'boolean' | 'date' | 'object';
  onChange: (items: any[]) => void;
  placeholder?: string;
  min?: number;
  max?: number;
  paramName?: string;  // For data attribute path building
  required?: boolean;  // If true, prevent deleting last item
}

export function ListInput({ items, itemType, onChange, placeholder, min, max, paramName, required }: ListInputProps) {
  const baseInputClass = "flex-1 rounded-lg border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600 text-sm";

  const addItem = () => {
    const defaultValue = itemType === 'number' ? 0 : itemType === 'boolean' ? false : itemType === 'object' ? {} : '';
    onChange([...items, defaultValue]);
  };

  const removeItem = (index: number) => {
    // Prevent removing last item if required
    if (required && items.length <= 1) return;
    onChange(items.filter((_, i) => i !== index));
  };

  const updateItem = (index: number, value: any) => {
    const newItems = [...items];
    newItems[index] = value;
    onChange(newItems);
  };

  const moveItem = (index: number, direction: 'up' | 'down') => {
    const newIndex = direction === 'up' ? index - 1 : index + 1;
    if (newIndex < 0 || newIndex >= items.length) return;
    const newItems = [...items];
    [newItems[index], newItems[newIndex]] = [newItems[newIndex], newItems[index]];
    onChange(newItems);
  };

  const renderItemInput = (item: any, index: number) => {
    if (itemType === 'boolean') {
      return (
        <label className="flex items-center gap-2 flex-1">
          <input
            type="checkbox"
            checked={item || false}
            onChange={(e) => updateItem(index, e.target.checked)}
            className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
            data-param-name={paramName ? `${paramName}[${index}]` : undefined}
          />
          <span className="text-sm text-gray-600">Item {index + 1}</span>
        </label>
      );
    }

    if (itemType === 'number') {
      return (
        <input
          type="number"
          value={item ?? ''}
          onChange={(e) => updateItem(index, e.target.value === '' ? '' : parseFloat(e.target.value))}
          className={baseInputClass}
          placeholder={placeholder || `Item ${index + 1}`}
          min={min}
          max={max}
          data-param-name={paramName ? `${paramName}[${index}]` : undefined}
        />
      );
    }

    if (itemType === 'date') {
      return (
        <input
          type="date"
          value={item || ''}
          onChange={(e) => updateItem(index, e.target.value)}
          className={baseInputClass}
          data-param-name={paramName ? `${paramName}[${index}]` : undefined}
        />
      );
    }

    // Handle object type with JSON textarea
    if (itemType === 'object') {
      return (
        <textarea
          value={typeof item === 'string' ? item : JSON.stringify(item || {}, null, 2)}
          onChange={(e) => {
            try {
              updateItem(index, JSON.parse(e.target.value));
            } catch {
              updateItem(index, e.target.value);
            }
          }}
          className={`${baseInputClass} font-mono text-xs`}
          placeholder={placeholder || '{ "key": "value" }'}
          rows={3}
          data-param-name={paramName ? `${paramName}[${index}]` : undefined}
        />
      );
    }

    // Default: string input
    return (
      <input
        type="text"
        value={item || ''}
        onChange={(e) => updateItem(index, e.target.value)}
        className={baseInputClass}
        placeholder={placeholder || `Item ${index + 1}`}
        data-param-name={paramName ? `${paramName}[${index}]` : undefined}
      />
    );
  };

  const canRemove = !required || items.length > 1;

  return (
    <div className="space-y-2" data-list-param={paramName}>
      {items.length === 0 ? (
        <div className="text-sm text-gray-500 italic py-2">No items added yet</div>
      ) : (
        <div className="space-y-2">
          {items.map((item, index) => (
            <div key={index} className="flex items-center gap-2 group" data-list-item={index}>
              {/* Reorder buttons */}
              <div className="flex flex-col gap-0.5">
                <button
                  type="button"
                  onClick={() => moveItem(index, 'up')}
                  disabled={index === 0}
                  className="p-0.5 text-gray-400 hover:text-gray-600 disabled:opacity-30 disabled:cursor-not-allowed"
                  title="Move up"
                >
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={() => moveItem(index, 'down')}
                  disabled={index === items.length - 1}
                  className="p-0.5 text-gray-400 hover:text-gray-600 disabled:opacity-30 disabled:cursor-not-allowed"
                  title="Move down"
                >
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
              </div>

              {/* Item input */}
              {renderItemInput(item, index)}

              {/* Remove button - hidden when required and only 1 item */}
              {canRemove && (
                <button
                  type="button"
                  onClick={() => removeItem(index)}
                  className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded transition-colors"
                  title="Remove item"
                  data-list-remove={index}
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Add button */}
      <button
        type="button"
        onClick={addItem}
        className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-indigo-700 bg-indigo-50 rounded-lg hover:bg-indigo-100 transition-colors"
        data-list-add={paramName}
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
        </svg>
        Add Item
      </button>
    </div>
  );
}

interface ObjectInputProps {
  fields: any[];
  value: Record<string, any>;
  onChange: (value: Record<string, any>) => void;
  level?: number;
  paramName?: string;  // For data attribute path building
}

export function ObjectInput({ fields, value, onChange, level = 0, paramName }: ObjectInputProps) {
  const baseInputClass = "w-full rounded-lg border border-gray-300 px-3 py-2 shadow-sm focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600 text-sm";

  const handleFieldChange = (fieldName: string, fieldValue: any) => {
    onChange({ ...value, [fieldName]: fieldValue });
  };

  // Build nested param name path
  const getFieldParamName = (fieldName: string) => {
    return paramName ? `${paramName}.${fieldName}` : fieldName;
  };

  const renderFieldInput = (field: any) => {
    const fieldValue = value?.[field.name] ?? (field.default !== undefined ? field.default : '');
    const fieldParamName = getFieldParamName(field.name);

    // Handle list of enum values as multi-select checkboxes
    if (field.isList && field.enum) {
      const selectedValues = Array.isArray(fieldValue) ? fieldValue : [];
      return (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            {field.enum.map((option: string) => {
              const isSelected = selectedValues.includes(option);
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
                      handleFieldChange(field.name, newValues);
                    }}
                    className="sr-only"
                    data-param-name={fieldParamName}
                  />
                  <span className="text-sm">{option}</span>
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

    // Resolve fields from either direct fields or modelRef
    const resolvedFields = field.fields || (field.modelRef ? resolveParamFields(field) : null);

    // Handle list fields
    if (field.isList) {
      // For object lists with field definitions, use ObjectListInput
      if (field.type === 'object' && resolvedFields) {
        return (
          <ObjectListInput
            fields={resolvedFields}
            items={Array.isArray(fieldValue) ? fieldValue : []}
            onChange={(items) => handleFieldChange(field.name, items)}
            level={level + 1}
            paramName={fieldParamName}
            required={field.required}
          />
        );
      }
      // For primitive lists, use ListInput
      const listValue = Array.isArray(fieldValue) ? fieldValue : [];
      return (
        <ListInput
          items={listValue}
          itemType={field.type}
          onChange={(items) => handleFieldChange(field.name, items)}
          min={field.min}
          max={field.max}
          paramName={fieldParamName}
          required={field.required}
        />
      );
    }

    // Handle nested objects with field definitions (direct or via modelRef)
    if (field.type === 'object' && resolvedFields) {
      return (
        <ObjectInput
          fields={resolvedFields}
          value={typeof fieldValue === 'object' && fieldValue !== null ? fieldValue : {}}
          onChange={(v) => handleFieldChange(field.name, v)}
          level={level + 1}
          paramName={fieldParamName}
        />
      );
    }

    // Handle enums as dropdowns
    if (field.enum) {
      return (
        <select
          value={fieldValue}
          onChange={(e) => handleFieldChange(field.name, e.target.value)}
          className={baseInputClass}
          required={field.required}
          data-param-name={fieldParamName}
        >
          <option value="">Select {field.label}</option>
          {field.enum.map((option: string) => (
            <option key={option} value={option}>{option}</option>
          ))}
        </select>
      );
    }

    // Handle boolean
    if (field.type === 'boolean') {
      return (
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={fieldValue || false}
            onChange={(e) => handleFieldChange(field.name, e.target.checked)}
            className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
            data-param-name={fieldParamName}
          />
          <span className="text-sm text-gray-500">Enable</span>
        </label>
      );
    }

    // Handle number
    if (field.type === 'number') {
      return (
        <input
          type="number"
          value={fieldValue ?? ''}
          onChange={(e) => handleFieldChange(field.name, e.target.value === '' ? '' : parseFloat(e.target.value))}
          className={baseInputClass}
          required={field.required}
          min={field.min}
          max={field.max}
          data-param-name={fieldParamName}
        />
      );
    }

    // Handle date
    if (field.type === 'date') {
      return (
        <input
          type="date"
          value={fieldValue || ''}
          onChange={(e) => handleFieldChange(field.name, e.target.value)}
          className={baseInputClass}
          required={field.required}
          data-param-name={fieldParamName}
        />
      );
    }

    // Handle object type without field definitions - fall back to JSON textarea
    if (field.type === 'object') {
      return (
        <div>
          <textarea
            data-param-name={fieldParamName}
            value={typeof fieldValue === 'string' ? fieldValue : JSON.stringify(fieldValue || {}, null, 2)}
            onChange={(e) => {
              try {
                handleFieldChange(field.name, JSON.parse(e.target.value));
              } catch {
                handleFieldChange(field.name, e.target.value);
              }
            }}
            className={`${baseInputClass} font-mono text-xs`}
            placeholder={'{\n  "key": "value"\n}'}
            rows={4}
            required={field.required}
          />
          <p className="mt-1 text-xs text-gray-500">Enter JSON object</p>
        </div>
      );
    }

    // Default: string input
    return (
      <input
        type="text"
        value={fieldValue || ''}
        onChange={(e) => handleFieldChange(field.name, e.target.value)}
        className={baseInputClass}
        required={field.required}
        minLength={field.minLength}
        maxLength={field.maxLength}
        data-param-name={fieldParamName}
      />
    );
  };

  const borderColors = ['border-gray-200', 'border-blue-200', 'border-purple-200', 'border-green-200'];
  const bgColors = ['bg-gray-50', 'bg-blue-50', 'bg-purple-50', 'bg-green-50'];

  return (
    <div className={`rounded-lg border ${borderColors[level % borderColors.length]} ${bgColors[level % bgColors.length]} p-3 space-y-3`}>
      {fields.map((field) => (
        <div key={field.name}>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            {field.label}
            {field.required && <span className="text-red-500 ml-1">*</span>}
          </label>
          {field.description && (
            <p className="text-xs text-gray-500 mb-1">{field.description}</p>
          )}
          {renderFieldInput(field)}
        </div>
      ))}
    </div>
  );
}

interface ObjectListInputProps {
  fields: any[];
  items: Record<string, any>[];
  onChange: (items: Record<string, any>[]) => void;
  level?: number;
  paramName?: string;  // For data attribute path building
  required?: boolean;  // If true, prevent deleting last item
}

export function ObjectListInput({ fields, items, onChange, level = 0, paramName, required }: ObjectListInputProps) {
  const addItem = () => {
    // Create empty object with default values
    const newItem: Record<string, any> = {};
    fields.forEach(field => {
      if (field.default !== undefined) {
        newItem[field.name] = field.default;
      } else if (field.isList) {
        // List fields should default to empty array (check before type-specific defaults)
        newItem[field.name] = [];
      } else if (field.type === 'boolean') {
        newItem[field.name] = false;
      } else if (field.type === 'number') {
        newItem[field.name] = 0;
      } else if (field.type === 'object') {
        // Object fields should default to empty object
        newItem[field.name] = {};
      } else {
        newItem[field.name] = '';
      }
    });
    onChange([...items, newItem]);
  };

  const removeItem = (index: number) => {
    // Prevent removing last item if required
    if (required && items.length <= 1) return;
    onChange(items.filter((_, i) => i !== index));
  };

  const updateItem = (index: number, value: Record<string, any>) => {
    const newItems = [...items];
    newItems[index] = value;
    onChange(newItems);
  };

  const moveItem = (index: number, direction: 'up' | 'down') => {
    const newIndex = direction === 'up' ? index - 1 : index + 1;
    if (newIndex < 0 || newIndex >= items.length) return;
    const newItems = [...items];
    [newItems[index], newItems[newIndex]] = [newItems[newIndex], newItems[index]];
    onChange(newItems);
  };

  const canRemove = !required || items.length > 1;

  return (
    <div className="space-y-2" data-list-param={paramName}>
      {items.length === 0 ? (
        <div className="text-sm text-gray-500 italic py-2">No items added yet</div>
      ) : (
        <div className="space-y-3">
          {items.map((item, index) => (
            <div key={index} className="flex gap-2" data-list-item={index}>
              {/* Reorder buttons */}
              <div className="flex flex-col gap-0.5 pt-1">
                <button
                  type="button"
                  onClick={() => moveItem(index, 'up')}
                  disabled={index === 0}
                  className="p-0.5 text-gray-400 hover:text-gray-600 disabled:opacity-30 disabled:cursor-not-allowed"
                  title="Move up"
                >
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 15l7-7 7 7" />
                  </svg>
                </button>
                <button
                  type="button"
                  onClick={() => moveItem(index, 'down')}
                  disabled={index === items.length - 1}
                  className="p-0.5 text-gray-400 hover:text-gray-600 disabled:opacity-30 disabled:cursor-not-allowed"
                  title="Move down"
                >
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
              </div>

              {/* Object fields */}
              <div className="flex-1">
                <div className="text-xs font-medium text-gray-500 mb-1">Item {index + 1}</div>
                <ObjectInput
                  fields={fields}
                  value={item}
                  onChange={(v) => updateItem(index, v)}
                  level={level}
                  paramName={paramName ? `${paramName}[${index}]` : undefined}
                />
              </div>

              {/* Remove button - hidden when required and only 1 item */}
              {canRemove && (
                <button
                  type="button"
                  onClick={() => removeItem(index)}
                  className="p-1.5 text-gray-400 hover:text-red-600 hover:bg-red-50 rounded transition-colors self-start mt-1"
                  title="Remove item"
                  data-list-remove={index}
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Add button */}
      <button
        type="button"
        onClick={addItem}
        className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-indigo-700 bg-indigo-50 rounded-lg hover:bg-indigo-100 transition-colors"
        data-list-add={paramName}
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
        </svg>
        Add Item
      </button>
    </div>
  );
}

// Re-export types for external use
export type { ListInputProps, ObjectInputProps, ObjectListInputProps };
