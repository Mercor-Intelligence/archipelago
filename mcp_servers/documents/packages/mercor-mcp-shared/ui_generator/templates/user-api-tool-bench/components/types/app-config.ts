/**
 * App Configuration Types
 *
 * These types define the configuration structure for HR app UIs.
 * Each app (Greenhouse, BambooHR, Workday) provides their own config
 * implementing this interface.
 */

// Schema types from /schema endpoint
export interface ColumnSchema {
  name: string;
  type: string;
  nullable: boolean;
  is_primary_key: boolean;
  is_foreign_key: boolean;
  fk_target: string | null;
  required: boolean;
  enum_values: string[] | null;
  is_unique: boolean;
  date_after: string | null;  // This date must be after the referenced column
}

export interface TableSchema {
  name: string;
  columns: ColumnSchema[];
  primary_keys: string[];
  foreign_keys: Record<string, string>;
  required_columns: string[];
  unique_constraints: string[][];  // List of column groups that must be unique
}

export interface SchemaResponse {
  tables: TableSchema[];
  import_order: string[];
  table_count: number;
}

// App-specific configuration
export interface ScenarioConfig {
  label: string;
  description: string;
  prompt: string;
}

export interface AppConfig {
  // App identity
  name: string;
  description: string;

  // Scenario presets for data generation
  scenarios: Record<string, ScenarioConfig>;
}

// NOTE: The following now come from /schema endpoint (SQLAlchemy model info dict):
// - enum_values: info={"enum": ["a", "b", "c"]}
// - unique_constraints: unique=True or UniqueConstraint(...)
// - date_after: info={"date_after": "column_name" or "table.column"}
