import { useState, useCallback, useEffect } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import axios from 'axios';
import { getApiBase } from '@mcp-shared/utils/api';
import type { AppConfig, TableSchema, SchemaResponse } from '../types/app-config';

interface DataGeneratorPageProps {
  config: AppConfig;
}

// Generate schema reference from fetched schema
function generateSchemaReference(schema: TableSchema[], importOrder: string[]): string {
  if (schema.length === 0) {
    return '(Schema loading...)';
  }

  const orderedTables = importOrder
    .map(name => schema.find(t => t.name === name))
    .filter((t): t is TableSchema => t !== undefined);

  const lines: string[] = [];
  orderedTables.forEach((table, idx) => {
    const columnDescs = table.columns.map(col => {
      const parts: string[] = [col.name];

      const typeMap: Record<string, string> = {
        'int': 'int',
        'str': 'string',
        'bool': 'bool',
        'float': 'float',
        'datetime': 'ISO8601',
        'dict': 'JSON',
      };
      parts.push(`(${typeMap[col.type] || col.type}`);

      const mods: string[] = [];
      if (col.required) mods.push('required');
      if (col.is_foreign_key && col.fk_target) {
        const [targetTable] = col.fk_target.split('.');
        mods.push(`FK->${targetTable}`);
      }
      if (!col.nullable && !col.required && !col.is_primary_key) mods.push('not null');
      if (col.nullable && !col.is_primary_key) mods.push('optional');

      if (mods.length > 0) {
        parts.push(`, ${mods.join(', ')}`);
      }
      parts.push(')');

      return parts.join('');
    });

    lines.push(`${idx + 1}. **${table.name}**: ${columnDescs.join(', ')}`);
  });

  return lines.join('\n');
}

// Generate FK rules from schema
function generateFKRules(schema: TableSchema[]): string {
  const rules: string[] = [];

  schema.forEach(table => {
    Object.entries(table.foreign_keys).forEach(([col, target]) => {
      const [targetTable] = target.split('.');
      if (targetTable === table.name) {
        rules.push(`   - \`${table.name}.${col}\` -> \`${table.name}.id\` (or empty for top-level)`);
      } else {
        rules.push(`   - \`${table.name}.${col}\` -> \`${targetTable}.id\``);
      }
    });
  });

  return rules.join('\n');
}

// Generate enum rules from schema
function generateEnumRules(schema: TableSchema[]): string {
  const rules: string[] = [];

  schema.forEach(table => {
    table.columns.forEach(col => {
      if (col.enum_values && col.enum_values.length > 0) {
        rules.push(`   - \`${table.name}.${col.name}\`: "${col.enum_values.join('", "')}"`);
      }
    });
  });

  return rules.join('\n');
}

// Helper to extract all enums from schema for display
function getEnumsFromSchema(schema: TableSchema[]): Record<string, string[]> {
  const enums: Record<string, string[]> = {};

  schema.forEach(table => {
    table.columns.forEach(col => {
      if (col.enum_values && col.enum_values.length > 0) {
        enums[`${table.name}.${col.name}`] = col.enum_values;
      }
    });
  });

  return enums;
}

// Generate date rules from schema
function generateDateRules(schema: TableSchema[]): string {
  const rules: string[] = [];

  schema.forEach(table => {
    table.columns.forEach(col => {
      if (col.date_after) {
        // date_after can be "column_name" (same table) or "table.column" (cross-table)
        const afterRef = col.date_after.includes('.')
          ? col.date_after
          : `${table.name}.${col.date_after}`;
        rules.push(`   - \`${table.name}.${col.name}\` should be after \`${afterRef}\``);
      }
    });
  });

  if (rules.length === 0) {
    return '   - (no date ordering rules defined)';
  }

  return rules.join('\n');
}

// Generate unique constraints from schema
function generateUniqueConstraints(schema: TableSchema[]): string {
  const constraints: string[] = [];

  schema.forEach(table => {
    table.unique_constraints.forEach(cols => {
      if (cols.length === 1) {
        constraints.push(`\`${table.name}.${cols[0]}\``);
      } else {
        // Composite unique constraint
        constraints.push(`(\`${table.name}.${cols.join('`, `' + table.name + '.')}\`)`);
      }
    });
  });

  if (constraints.length === 0) {
    return '(none)';
  }

  return constraints.join(', ');
}

// Main prompt generator
function generateMainPrompt(scenario: string, schema: TableSchema[], importOrder: string[], config: AppConfig): string {
  const schemaReference = generateSchemaReference(schema, importOrder);
  const fkRules = generateFKRules(schema);
  const enumRules = generateEnumRules(schema);
  const dateRules = generateDateRules(schema);
  const uniqueConstraints = generateUniqueConstraints(schema);
  const tableCount = schema.length;

  return `I need you to generate realistic sample data for a ${config.name} ${config.description} database.

**My Scenario:**
${scenario}

**CRITICAL RULES:**

1. **Foreign Key Consistency**: Every FK reference MUST point to an ID that exists:
${fkRules}

2. **Self-Referential Hierarchies**: Parent records must have LOWER id numbers than children.

3. **Date Logic**:
${dateRules}

4. **Unique Constraints**: ${uniqueConstraints} must be unique.

5. **Valid Enum Values**:
${enumRules}

**OUTPUT FORMAT:**
Use Python to generate the CSV files and provide download links for each file.
- Use empty strings (not "null" or "None") for nullable fields
- Use lowercase "true"/"false" for booleans
- Use ISO 8601 format for timestamps (e.g., "2024-07-15T10:30:00Z")
- Create each table as a separate CSV file
- After generating all files, provide a ZIP download containing all CSVs

**SCHEMA REFERENCE (${tableCount} Tables in dependency order):**

${schemaReference}

Now generate the CSV files for my scenario.`;
}

// Copy button component
function CopyButton({ text, label = 'Copy', successLabel = 'Copied!' }: { text: string; label?: string; successLabel?: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  }, [text]);

  return (
    <button
      onClick={handleCopy}
      className={`
        inline-flex items-center gap-2 px-4 py-2 rounded-md font-medium transition-all duration-200 text-sm
        ${copied
          ? 'bg-gray-900 text-white'
          : 'bg-gray-900 hover:bg-gray-800 text-white'
        }
      `}
    >
      {copied ? (
        <>
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
          {successLabel}
        </>
      ) : (
        <>
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
          </svg>
          {label}
        </>
      )}
    </button>
  );
}

// Collapsible section component
function CollapsibleSection({ title, children, defaultOpen = false }: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [isOpen, setIsOpen] = useState(defaultOpen);

  return (
    <div className="border border-gray-200 rounded-lg overflow-hidden">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between px-4 py-3 bg-gray-50 hover:bg-gray-100 transition-colors text-left"
      >
        <span className="font-medium text-gray-900">{title}</span>
        <svg
          className={`w-5 h-5 text-gray-500 transition-transform ${isOpen ? 'rotate-180' : ''}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {isOpen && (
        <div className="p-4 bg-white border-t border-gray-200">
          {children}
        </div>
      )}
    </div>
  );
}

// Phase card component
function PhaseCard({ phase }: { phase: AppConfig['optionalPhases'][0] }) {
  const [showPrompt, setShowPrompt] = useState(false);

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
      <div className="p-4 space-y-3">
        <div className="flex items-start justify-between gap-4">
          <div className="flex-1 min-w-0">
            <h4 className="font-medium text-gray-900">{phase.title}</h4>
            <div className="mt-1 flex flex-wrap gap-1">
              {phase.tables.map(table => (
                <span key={table} className="inline-block px-2 py-0.5 bg-gray-100 text-gray-600 text-xs rounded">
                  {table}
                </span>
              ))}
            </div>
          </div>
          <CopyButton text={phase.prompt} label="Copy" />
        </div>
        <button
          onClick={() => setShowPrompt(!showPrompt)}
          className="flex items-center gap-1 text-xs text-gray-500 hover:text-gray-700 transition-colors"
        >
          <svg
            className={`w-4 h-4 transition-transform ${showPrompt ? 'rotate-180' : ''}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
          {showPrompt ? 'Hide prompt' : 'Preview prompt'}
        </button>
      </div>
      {showPrompt && (
        <div className="px-4 pb-4">
          <pre className="text-xs text-gray-700 whitespace-pre-wrap bg-gray-50 p-3 rounded-lg max-h-48 overflow-y-auto font-mono border border-gray-100">
            {phase.prompt}
          </pre>
        </div>
      )}
    </div>
  );
}

export default function DataGeneratorPage({ config }: DataGeneratorPageProps) {
  const scenarios = config.scenarios;
  type ScenarioKey = keyof typeof scenarios;

  const [selectedScenario, setSelectedScenario] = useState<ScenarioKey>(Object.keys(scenarios)[0] as ScenarioKey);
  const [customScenario, setCustomScenario] = useState('');
  const [schema, setSchema] = useState<TableSchema[]>([]);
  const [importOrder, setImportOrder] = useState<string[]>([]);
  const [schemaLoading, setSchemaLoading] = useState(true);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  // Fetch schema on mount
  useEffect(() => {
    const fetchSchema = async () => {
      try {
        const apiBase = getApiBase();
        const response = await axios.get<SchemaResponse>(`${apiBase}/schema`);
        setSchema(response.data.tables);
        setImportOrder(response.data.import_order);
        setSchemaError(null);
      } catch (err) {
        console.error('Failed to fetch schema:', err);
        setSchemaError('Failed to load database schema. Please check that the server is running.');
      } finally {
        setSchemaLoading(false);
      }
    };
    fetchSchema();
  }, []);

  const currentScenario = selectedScenario === 'custom'
    ? customScenario
    : scenarios[selectedScenario]?.prompt || '';

  const mainPrompt = generateMainPrompt(currentScenario, schema, importOrder, config);

  return (
    <>
      <Head>
        <title>Data Generator - {config.name}</title>
        <meta name="description" content={`Generate sample data for ${config.name} ${config.description} using ChatGPT`} />
      </Head>

      <div className="min-h-screen bg-gray-50">
        {/* Header */}
        <header className="bg-white border-b border-gray-200 sticky top-0 z-10">
          <div className="max-w-5xl mx-auto px-4 py-4">
            <div className="flex items-center justify-between">
              <div>
                <h1 className="text-xl font-semibold text-gray-900">Data Generator</h1>
                <p className="text-sm text-gray-500">Generate sample data for {config.name} using ChatGPT</p>
              </div>
              <Link
                href="/"
                className="text-sm text-blue-600 hover:text-blue-800 flex items-center gap-1"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 19l-7-7m0 0l7-7m-7 7h18" />
                </svg>
                Back to Tools
              </Link>
            </div>
          </div>
        </header>

        <main className="max-w-5xl mx-auto px-4 py-8 space-y-8">
          {/* Step 1: Choose Scenario */}
          <section className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            <div className="px-6 py-3 border-b border-gray-100 bg-gray-50">
              <div className="flex items-center gap-3">
                <span className="flex items-center justify-center w-6 h-6 bg-gray-900 rounded text-white text-xs font-medium">1</span>
                <h2 className="text-sm font-medium text-gray-900">Choose Your Scenario</h2>
              </div>
            </div>
            <div className="p-6 space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {(Object.keys(scenarios) as ScenarioKey[]).map((key) => (
                  <button
                    key={key}
                    onClick={() => setSelectedScenario(key)}
                    className={`
                      text-left p-3 rounded-md border transition-all
                      ${selectedScenario === key
                        ? 'border-gray-900 bg-gray-50'
                        : 'border-gray-200 hover:border-gray-300'
                      }
                    `}
                  >
                    <div className="text-sm font-medium text-gray-900">{scenarios[key].label}</div>
                    <div className="text-xs text-gray-500 mt-1">{scenarios[key].description}</div>
                  </button>
                ))}
              </div>

              {selectedScenario === 'custom' && (
                <div className="mt-4">
                  <label className="block text-sm font-medium text-gray-700 mb-2">
                    Describe your scenario
                  </label>
                  <textarea
                    value={customScenario}
                    onChange={(e) => setCustomScenario(e.target.value)}
                    placeholder="E.g., A healthcare company with 10 nursing positions, 50 candidates from various sources..."
                    className="w-full px-4 py-3 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 resize-none"
                    rows={3}
                  />
                </div>
              )}
            </div>
          </section>

          {/* Step 2: Copy the Prompt */}
          <section className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            <div className="px-6 py-3 border-b border-gray-100 bg-gray-50">
              <div className="flex items-center gap-3">
                <span className="flex items-center justify-center w-6 h-6 bg-gray-900 rounded text-white text-xs font-medium">2</span>
                <h2 className="text-sm font-medium text-gray-900">Copy & Paste into ChatGPT</h2>
              </div>
            </div>
            <div className="p-6 space-y-4">
              <div className="flex items-center justify-between gap-4 p-4 bg-gray-50 border border-gray-200 rounded-lg">
                <div>
                  <p className={`font-medium ${schemaError ? 'text-red-600' : 'text-gray-900'}`}>
                    {schemaLoading ? 'Loading schema...' : schemaError ? schemaError : `Ready to generate ${schema.length} tables`}
                  </p>
                  <p className="text-sm text-gray-500 mt-1">
                    {schemaError
                      ? 'Cannot generate prompts without schema information'
                      : currentScenario
                        ? `Scenario: "${currentScenario.slice(0, 60)}${currentScenario.length > 60 ? '...' : ''}"`
                        : 'Please enter a custom scenario above'
                    }
                  </p>
                </div>
                <CopyButton
                  text={mainPrompt}
                  label="Copy Prompt"
                  successLabel="Copied!"
                />
              </div>

              <CollapsibleSection title="Preview prompt">
                <pre className="text-xs text-gray-700 whitespace-pre-wrap bg-gray-50 p-4 rounded-lg max-h-96 overflow-y-auto font-mono">
                  {mainPrompt}
                </pre>
              </CollapsibleSection>

              {!schemaError && (
                <p className="text-sm text-gray-500">
                  Open <a href="https://chat.openai.com" target="_blank" rel="noopener noreferrer" className="text-gray-700 underline hover:no-underline">ChatGPT</a>, paste this prompt, and it will generate Python code that creates a downloadable zip file with all {schema.length} required tables.
                </p>
              )}
            </div>
          </section>

          {/* Step 3: Optional Tables */}
          {config.optionalPhases.length > 0 && (
            <section className="bg-white rounded-lg border border-gray-200 overflow-hidden">
              <div className="px-6 py-3 border-b border-gray-100 bg-gray-50">
                <div className="flex items-center gap-3">
                  <span className="flex items-center justify-center w-6 h-6 bg-gray-900 rounded text-white text-xs font-medium">3</span>
                  <h2 className="text-sm font-medium text-gray-900">
                    Add Optional Tables ({config.optionalPhases.reduce((sum, p) => sum + p.tables.length, 0)} more)
                  </h2>
                </div>
              </div>
              <div className="p-6 space-y-4">
                <p className="text-gray-600">
                  After generating the required tables, you can ask ChatGPT for additional tables in phases. Copy each phase prompt to expand your dataset.
                </p>

                <div className="space-y-3">
                  {config.optionalPhases.map((phase) => (
                    <PhaseCard key={phase.id} phase={phase} />
                  ))}
                </div>
              </div>
            </section>
          )}

          {/* Quick Reference */}
          <section className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            <div className="px-6 py-3 border-b border-gray-100 bg-gray-50">
              <h2 className="text-sm font-medium text-gray-900">Quick Reference</h2>
            </div>
            <div className="p-6">
              <CollapsibleSection title={`All Tables (${schema.length})`}>
                <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
                  {schemaLoading ? (
                    <div className="col-span-3 text-gray-500 text-sm">Loading schema...</div>
                  ) : (
                    importOrder.map(table => (
                      <div key={table} className="px-2 py-1.5 bg-gray-50 text-gray-700 text-xs rounded border border-gray-200 font-mono">
                        {table}
                      </div>
                    ))
                  )}
                </div>
              </CollapsibleSection>

              <div className="mt-4">
                <CollapsibleSection title="Valid Enum Values">
                  <div className="space-y-3 text-sm">
                    {schemaLoading ? (
                      <div className="text-gray-500">Loading schema...</div>
                    ) : (
                      Object.entries(getEnumsFromSchema(schema)).map(([field, values]) => (
                        <div key={field}>
                          <span className="font-medium text-gray-700">{field}:</span>
                          <span className="ml-2 text-gray-600">"{values.join('", "')}"</span>
                        </div>
                      ))
                    )}
                  </div>
                </CollapsibleSection>
              </div>

              <div className="mt-4">
                <CollapsibleSection title="CSV Format Rules">
                  <ul className="space-y-2 text-sm text-gray-600">
                    <li className="flex items-start gap-2">
                      <span className="text-gray-400">•</span>
                      Use empty strings (not "null" or "None") for nullable fields
                    </li>
                    <li className="flex items-start gap-2">
                      <span className="text-gray-400">•</span>
                      Use lowercase "true"/"false" for booleans
                    </li>
                    <li className="flex items-start gap-2">
                      <span className="text-gray-400">•</span>
                      Use ISO 8601 format for timestamps (e.g., "2024-07-15T10:30:00Z")
                    </li>
                    <li className="flex items-start gap-2">
                      <span className="text-gray-400">•</span>
                      Parent IDs must be lower than child IDs for self-referential tables
                    </li>
                  </ul>
                </CollapsibleSection>
              </div>
            </div>
          </section>
        </main>
      </div>
    </>
  );
}

export type { DataGeneratorPageProps };
