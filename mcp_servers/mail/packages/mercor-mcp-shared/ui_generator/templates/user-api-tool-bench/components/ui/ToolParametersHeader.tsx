import { DataType } from '@/lib/api-config';

export interface ToolParametersHeaderProps {
  dataType: DataType;
}

/**
 * Extension point rendered between the "Parameters" heading and the parameter inputs.
 * Override this component to add tool-specific tips, info boxes, or other content
 * that should appear at the top of the parameters section.
 *
 * Default: renders nothing.
 */
export default function ToolParametersHeader(_props: ToolParametersHeaderProps) {
  return null;
}
