// Parameter label component - can be overridden to customize requirement display
import React from 'react';

export interface ParameterLabelProps {
  label: string;
  required: boolean;
  description?: string;
  /** Additional context about the parameter (e.g., from tool definition) */
  param?: any;
}

export default function ParameterLabel({ label, required, description }: ParameterLabelProps) {
  return (
    <>
      <label className="block text-sm font-medium text-gray-700">
        {label}
        {required ? (
          <span className="ml-2 text-xs text-red-600 font-semibold">Required</span>
        ) : (
          <span className="ml-2 text-xs text-gray-400">Optional</span>
        )}
      </label>
      {description && (
        <p className="text-xs text-gray-500">{description}</p>
      )}
    </>
  );
}
