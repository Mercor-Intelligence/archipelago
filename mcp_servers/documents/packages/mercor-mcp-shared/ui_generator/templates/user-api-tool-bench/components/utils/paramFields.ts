import { getModelByName } from '@/lib/api-config';

// Helper function to resolve fields from either direct fields or modelRef
// Returns null if no fields are available
export function resolveParamFields(param: any): any[] | null {
  // If fields are directly embedded, use them
  if (param.fields) {
    return param.fields;
  }

  // If modelRef is provided, look up the model and convert fields
  if (param.modelRef) {
    const model = getModelByName(param.modelRef);
    if (model && model.fields) {
      // Convert Model fields format to FieldDefinition format
      return Object.entries(model.fields).map(([fieldName, fieldDef]: [string, any]) => {
        // Use the type directly if it's already a UI type (string, number, boolean, object, array)
        const typeStr = fieldDef.type || 'string';
        let fieldType = typeStr;

        // Use pre-extracted enum values from the generator (preferred)
        // The generator extracts these using Python's get_args() from Literal types
        let enumValues: string[] | undefined = fieldDef.enum;

        // If the type looks like a Python type string, convert it
        // Otherwise, assume it's already a UI-compatible type
        if (typeStr.match(/Literal\[/) || typeStr.includes('|') || typeStr.includes('[')) {
          // Legacy Python-style type string - parse it
          fieldType = 'string';
          if (enumValues || typeStr.match(/Literal\[/)) {
            fieldType = 'string';
          } else if (typeStr.includes('int') || typeStr.includes('float') || typeStr.includes('number')) {
            fieldType = 'number';
          } else if (typeStr.includes('bool')) {
            fieldType = 'boolean';
          } else if (typeStr.includes('list') || typeStr.includes('array')) {
            fieldType = 'array';
          }
        }

        // Convert Python default values to JavaScript values
        let defaultValue: any = undefined;
        if (fieldDef.default !== 'None' && fieldDef.default !== null) {
          const defaultStr = String(fieldDef.default);
          if (defaultStr === 'True') {
            defaultValue = true;
          } else if (defaultStr === 'False') {
            defaultValue = false;
          } else if (defaultStr.match(/^-?\d+$/)) {
            // Integer
            defaultValue = parseInt(defaultStr, 10);
          } else if (defaultStr.match(/^-?\d+\.\d+$/)) {
            // Float
            defaultValue = parseFloat(defaultStr);
          } else if (defaultStr.startsWith('"') && defaultStr.endsWith('"')) {
            // String with double quotes
            defaultValue = defaultStr.slice(1, -1);
          } else if (defaultStr.startsWith("'") && defaultStr.endsWith("'")) {
            // String with single quotes
            defaultValue = defaultStr.slice(1, -1);
          } else {
            // Keep as-is for other cases
            defaultValue = fieldDef.default;
          }
        }

        const field: any = {
          name: fieldName,
          label: fieldName.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' '),
          type: fieldType,
          required: fieldDef.required || false,
          description: fieldDef.description || '',
          default: defaultValue,
          enum: enumValues,
        };

        // Copy over isList if present (for nested list rendering)
        if (fieldDef.isList) {
          field.isList = true;
        }

        // Copy over modelRef if present (for nested object types)
        if (fieldDef.modelRef) {
          field.modelRef = fieldDef.modelRef;
        }

        return field;
      });
    }
  }

  return null;
}
