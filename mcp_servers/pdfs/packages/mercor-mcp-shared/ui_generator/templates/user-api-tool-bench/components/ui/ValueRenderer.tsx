import React from 'react';

// Helper: detect if array should be rendered as a table
function isTableData(array: any[]): boolean {
  if (!Array.isArray(array) || array.length === 0) return false;

  // Must be array of objects
  if (!array.every(item => typeof item === 'object' && item !== null && !Array.isArray(item))) {
    return false;
  }

  // Get keys from first object
  const keys = Object.keys(array[0]);
  if (keys.length === 0 || keys.length > 10) return false;

  // Check all objects have same keys (consistent schema)
  const consistentSchema = array.every(item => {
    const itemKeys = Object.keys(item);
    return itemKeys.length === keys.length && keys.every(k => k in item);
  });

  if (!consistentSchema) return false;

  // Check values are "flat" (primitives or simple objects)
  const isFlat = array.every(item =>
    Object.values(item).every(val => {
      if (val === null || val === undefined) return true;
      if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean') return true;
      if (typeof val === 'object' && !Array.isArray(val)) {
        return Object.keys(val).length <= 3;
      }
      return false;
    })
  );

  return isFlat;
}

// Render array as HTML table
function renderTable(data: any[], renderValue: (value: any, depth: number, fieldName?: string) => React.ReactNode) {
  return (
    <div className="overflow-x-auto mt-2">
      <table className="min-w-full divide-y divide-gray-300 border border-gray-300 rounded-lg">
        <thead className="bg-gray-50">
          <tr>
            {Object.keys(data[0]).map(key => (
              <th key={key} className="px-4 py-3 text-left text-xs font-semibold text-gray-700 uppercase tracking-wider border-b border-gray-300">
                {key.replace(/_/g, ' ')}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="bg-white divide-y divide-gray-200">
          {data.map((row, idx) => (
            <tr key={idx} className="hover:bg-gray-50">
              {Object.entries(row).map(([key, val], i) => (
                <td key={i} className="px-4 py-3 text-sm">
                  {renderValue(val, 999, key)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Generic recursive value renderer for any data type
export function renderValue(value: any, depth: number = 0, fieldName?: string): React.ReactNode {
  // Null/undefined
  if (value === null || value === undefined) {
    return <span className="text-gray-400 italic text-sm">null</span>;
  }

  // String
  if (typeof value === 'string') {
    // Check for date/datetime fields
    const dateFields = ['date', 'created', 'updated', 'modified', 'timestamp', 'due_date', 'start_date', 'end_date', 'birthdate', 'birth_date', 'published', 'expires', 'expiry'];
    const isDateField = fieldName && dateFields.some(f => {
      const lowerFieldName = fieldName.toLowerCase();
      const pattern = f.toLowerCase();
      const regex = new RegExp(`(^|_)${pattern}($|_)`);
      return regex.test(lowerFieldName);
    });

    // Check if string matches ISO 8601 date/datetime patterns
    const isoDatePattern = /^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?(\.\d{1,9})?(Z|[+-]\d{2}:?\d{2})?)?$/;
    const isDateValue = isoDatePattern.test(value);

    // Format as date if field name suggests date or value matches date pattern
    if (isDateField || isDateValue) {
      try {
        let date: Date;
        const dateOnlyPattern = /^(\d{4})-(\d{2})-(\d{2})$/;
        const dateOnlyMatch = value.match(dateOnlyPattern);
        let isDateOnly = false;

        if (dateOnlyMatch) {
          const year = parseInt(dateOnlyMatch[1], 10);
          const month = parseInt(dateOnlyMatch[2], 10);
          const day = parseInt(dateOnlyMatch[3], 10);

          if (month < 1 || month > 12 || day < 1 || day > 31) {
            throw new Error('Invalid date range');
          }

          date = new Date(year, month - 1, day);

          if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
            throw new Error('Date rollover detected');
          }

          isDateOnly = true;
        } else if (!isDateValue && isDateField) {
          const dateSeparatorPattern = /^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{4})(\s+(\d{1,2}):(\d{2})(:(\d{2}))?)?$/;
          const dateMatch = value.match(dateSeparatorPattern);

          if (dateMatch) {
            const firstGroup = parseInt(dateMatch[1], 10);
            const secondGroup = parseInt(dateMatch[2], 10);
            const year = parseInt(dateMatch[3], 10);

            const hasTimeComponent = dateMatch[4] !== undefined;
            let hours = 0, minutes = 0, seconds = 0;
            if (hasTimeComponent && dateMatch[5]) {
              hours = parseInt(dateMatch[5], 10);
              minutes = parseInt(dateMatch[6], 10);
              seconds = dateMatch[8] ? parseInt(dateMatch[8], 10) : 0;
            }

            if (firstGroup < 1 || firstGroup > 31 || secondGroup < 1 || secondGroup > 31) {
              throw new Error('Invalid date range');
            }

            if (hasTimeComponent) {
              if (hours < 0 || hours > 23 || minutes < 0 || minutes > 59 || seconds < 0 || seconds > 59) {
                throw new Error('Invalid time range');
              }
            }

            const testDate1 = new Date(year, firstGroup - 1, secondGroup, hours, minutes, seconds);
            const testDate2 = new Date(year, secondGroup - 1, firstGroup, hours, minutes, seconds);

            const valid1 = testDate1.getFullYear() === year &&
                          testDate1.getMonth() === firstGroup - 1 &&
                          testDate1.getDate() === secondGroup;
            const valid2 = testDate2.getFullYear() === year &&
                          testDate2.getMonth() === secondGroup - 1 &&
                          testDate2.getDate() === firstGroup;

            if (!valid1 && !valid2) {
              throw new Error('Date rollover detected');
            }

            if (valid1 && valid2) {
              if (firstGroup <= 12 && secondGroup > 12) {
                date = testDate1;
              } else {
                date = testDate2;
              }
            } else {
              date = valid1 ? testDate1 : testDate2;
            }
            isDateOnly = !hasTimeComponent;
          } else {
            throw new Error('Not a recognizable date format');
          }
        } else {
          date = new Date(value);
        }

        if (!isNaN(date.getTime())) {
          const hasTime = !isDateOnly && (value.includes('T') || / \d{1,2}:\d{2}/.test(value) || value.length > 10);
          if (hasTime) {
            const formatted = date.toLocaleString('en-US', {
              year: 'numeric',
              month: 'short',
              day: 'numeric',
              hour: '2-digit',
              minute: '2-digit',
              second: '2-digit',
              hour12: true
            });
            return <span className="text-gray-900 font-medium">{formatted}</span>;
          } else {
            const formatted = isDateOnly
              ? date.toLocaleDateString('en-US', {
                  year: 'numeric',
                  month: 'short',
                  day: 'numeric'
                })
              : date.toLocaleDateString('en-US', {
                  year: 'numeric',
                  month: 'short',
                  day: 'numeric',
                  timeZone: 'UTC'
                });
            return <span className="text-gray-900 font-medium">{formatted}</span>;
          }
        }
      } catch (err) {
        // Fall through to regular string rendering
      }
    }

    // Truncate very long strings
    if (value.length > 500 && depth > 0) {
      return <span className="text-gray-800">{value.substring(0, 500)}... <span className="text-gray-400 text-xs">(truncated)</span></span>;
    }
    return <span className="text-gray-800">{value}</span>;
  }

  // Number
  if (typeof value === 'number') {
    const currencyFields = ['amount', 'total', 'subtotal', 'tax', 'price', 'cost', 'paid', 'due', 'balance', 'credit'];
    const isCurrency = fieldName && currencyFields.some(f => {
      const lowerFieldName = fieldName.toLowerCase();
      const pattern = f.toLowerCase();
      const regex = new RegExp(`(^|_)${pattern}($|_)`);
      return regex.test(lowerFieldName);
    });

    if (isCurrency) {
      const formatted = value.toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
      });
      return <span className="text-gray-900 font-semibold">{formatted}</span>;
    }

    const identifierFields = ['code', 'accountcode', 'id', 'number', 'line', 'index', 'count', 'page'];
    const isIdentifier = fieldName && identifierFields.some(f => {
      const lowerFieldName = fieldName.toLowerCase();
      const pattern = f.toLowerCase();
      const regex = new RegExp(`(^|_)${pattern}($|_)`);
      return regex.test(lowerFieldName);
    });

    if (isIdentifier) {
      return <span className="text-gray-900 font-mono">{value}</span>;
    }

    return <span className="text-gray-900">{value}</span>;
  }

  // Boolean
  if (typeof value === 'boolean') {
    return (
      <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${value ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>
        {value ? '✓ Yes' : '✗ No'}
      </span>
    );
  }

  // Array
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <span className="text-gray-400 italic text-sm">No items</span>;
    }

    if (isTableData(value)) {
      return renderTable(value, renderValue);
    }

    const isObjectArray = value.every(item => typeof item === 'object' && item !== null && !Array.isArray(item));

    if (isObjectArray) {
      return (
        <div className="space-y-3 mt-2">
          {value.map((item, idx) => (
            <div key={idx} className="bg-white border border-gray-200 rounded-lg p-3 shadow-sm">
              {renderValue(item, depth + 1)}
            </div>
          ))}
        </div>
      );
    }

    return (
      <div className="space-y-1 mt-1">
        {value.map((item, idx) => (
          <div key={idx} className="flex items-start gap-2 py-1">
            <span className="text-gray-400 text-xs mt-0.5">•</span>
            <div className="flex-1">{renderValue(item, depth + 1)}</div>
          </div>
        ))}
      </div>
    );
  }

  // Object
  if (typeof value === 'object') {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      return <span className="text-gray-400 italic text-sm">Empty</span>;
    }

    return (
      <div className={`grid gap-2 ${depth === 0 ? 'mt-2' : 'mt-1'}`}>
        {entries.map(([k, v]) => {
          const label = k.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
          const isNumber = typeof v === 'number';

          return (
            <div key={k} className={`${depth === 0 ? 'py-2 border-b border-gray-100 last:border-0' : ''}`}>
              <div className={`text-xs font-medium uppercase tracking-wide mb-1 ${
                depth === 0 ? 'text-indigo-700' : 'text-gray-600'
              }`}>
                {label}
              </div>
              <div className={`${isNumber ? 'text-base' : 'text-sm'}`}>
                {renderValue(v, depth + 1, k)}
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  // Fallback
  return <span className="text-gray-700">{String(value)}</span>;
}

interface ValueRendererProps {
  value: any;
  fieldName?: string;
}

export default function ValueRenderer({ value, fieldName }: ValueRendererProps) {
  return <>{renderValue(value, 0, fieldName)}</>;
}
