import { useState } from 'react'
import Head from 'next/head'
import Link from 'next/link'
import { ModelField, Model, models } from '../lib/api-config'
import pkg from '../package.json'

export default function ModelsDocumentation() {
  const [searchTerm, setSearchTerm] = useState('')
  const [selectedModel, setSelectedModel] = useState<string | null>(null)

  const filteredModels = models.filter(
    (model) =>
      model.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
      (model.docstring?.toLowerCase().includes(searchTerm.toLowerCase()) ?? false)
  )

  const getModelById = (name: string) => models.find((m) => m.name === name)

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-50 to-gray-100">
      <Head>
        <title>API Models Documentation - {pkg.name}</title>
        <meta name="description" content="API Models and Schema Documentation" />
        <link rel="icon" href="/favicon.ico" />
      </Head>

      {/* Navigation Bar */}
      <nav className="bg-white border-b border-gray-200 px-4 py-3">
        <div className="container mx-auto flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Link href="/" className="text-blue-600 hover:text-blue-700 transition-colors">
              ← Back to Tools
            </Link>
          </div>
          <div className="flex gap-4">
            <Link
              href="/"
              className="text-gray-700 hover:text-blue-600 transition-colors"
            >
              Tools
            </Link>
            <span className="text-blue-600 font-semibold">
              API Models
            </span>
          </div>
        </div>
      </nav>

      <main className="container mx-auto px-4 py-8">
        {/* Header */}
        <div className="mb-8">
          <h1 className="text-4xl font-bold text-gray-900 mb-2">
            API Models Documentation
          </h1>
          <p className="text-gray-600">
            Explore the data models and schemas used in this API
          </p>
        </div>

        {/* Search Bar */}
        <div className="mb-6">
          <input
            type="text"
            placeholder="Search models..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="w-full px-4 py-3 rounded-lg border border-gray-300 focus:ring-2 focus:ring-blue-500 focus:border-transparent"
          />
        </div>

        {/* Layout: Sidebar + Content */}
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
          {/* Sidebar - Model List */}
          <div className="lg:col-span-1">
            <div className="bg-white rounded-lg shadow-md p-4 sticky top-4 max-h-[calc(100vh-8rem)] overflow-y-auto">
              <h2 className="text-lg font-semibold mb-4 text-gray-900">
                Models ({filteredModels.length})
              </h2>
              <div className="space-y-1">
                {filteredModels.map((model) => (
                  <button
                    key={model.name}
                    onClick={() => setSelectedModel(model.name)}
                    className={`w-full text-left px-3 py-2 rounded-md transition-colors ${
                      selectedModel === model.name
                        ? 'bg-blue-100 text-blue-900 font-medium'
                        : 'hover:bg-gray-100 text-gray-700'
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
              <ModelDetail model={getModelById(selectedModel)!} />
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
      </main>
    </div>
  )
}

function ModelCard({ model, onClick }: { model: Model; onClick: () => void }) {
  const fieldCount = Object.keys(model.fields).length
  const requiredCount = Object.values(model.fields).filter((f) => f.required).length

  return (
    <div
      onClick={onClick}
      className="bg-white rounded-lg shadow-md p-6 hover:shadow-lg transition-shadow cursor-pointer border border-gray-200"
    >
      <div className="flex items-start justify-between mb-3">
        <h3 className="text-xl font-bold text-gray-900">{model.name}</h3>
        {model.is_enum && (
          <span className="px-3 py-1 text-sm bg-purple-100 text-purple-700 rounded-full">
            Enum
          </span>
        )}
      </div>

      {model.docstring && (
        <p className="text-gray-600 text-sm mb-4 line-clamp-2">{model.docstring}</p>
      )}

      <div className="flex items-center gap-4 text-sm text-gray-500">
        <span>
          <strong>{fieldCount}</strong> {fieldCount === 1 ? 'field' : 'fields'}
        </span>
        {!model.is_enum && (
          <span>
            <strong>{requiredCount}</strong> required
          </span>
        )}
      </div>

      {model.bases.length > 0 && (
        <div className="mt-3 pt-3 border-t border-gray-200">
          <span className="text-xs text-gray-500">
            Extends: {model.bases.join(', ')}
          </span>
        </div>
      )}
    </div>
  )
}

function ModelDetail({ model }: { model: Model }) {
  return (
    <div className="bg-white rounded-lg shadow-md p-8">
      {/* Header */}
      <div className="mb-6 pb-6 border-b border-gray-200">
        <div className="flex items-center gap-3 mb-3">
          <h2 className="text-3xl font-bold text-gray-900">{model.name}</h2>
          {model.is_enum && (
            <span className="px-3 py-1 text-sm bg-purple-100 text-purple-700 rounded-full">
              Enum
            </span>
          )}
        </div>

        {model.docstring && (
          <p className="text-gray-600 whitespace-pre-wrap">{model.docstring}</p>
        )}

        {model.bases.length > 0 && (
          <div className="mt-4">
            <span className="text-sm text-gray-500">
              Extends: <code className="text-blue-600">{model.bases.join(', ')}</code>
            </span>
          </div>
        )}
      </div>

      {/* Fields */}
      <div>
        <h3 className="text-xl font-semibold mb-4 text-gray-900">
          {model.is_enum ? 'Values' : 'Fields'}
        </h3>
        <div className="space-y-4">
          {Object.entries(model.fields).map(([fieldName, field]) => (
            <FieldRow key={fieldName} name={fieldName} field={field} isEnum={model.is_enum} />
          ))}
        </div>
      </div>

      {/* JSON Example */}
      {!model.is_enum && (
        <div className="mt-8 pt-8 border-t border-gray-200">
          <h3 className="text-xl font-semibold mb-4 text-gray-900">Example JSON</h3>
          <pre className="bg-gray-50 rounded-lg p-4 overflow-x-auto text-sm">
            <code>{generateExampleJSON(model)}</code>
          </pre>
        </div>
      )}
    </div>
  )
}

function FieldRow({
  name,
  field,
  isEnum,
}: {
  name: string
  field: ModelField
  isEnum: boolean
}) {
  return (
    <div className="border border-gray-200 rounded-lg p-4 hover:border-blue-300 transition-colors">
      <div className="flex items-start justify-between mb-2">
        <div className="flex items-center gap-2">
          <code className="text-blue-600 font-semibold">{name}</code>
          {field.required && !isEnum && (
            <span className="px-2 py-0.5 text-xs bg-red-100 text-red-700 rounded">
              Required
            </span>
          )}
        </div>
        <code className="text-sm text-gray-600 bg-gray-100 px-2 py-1 rounded">
          {field.type}
        </code>
      </div>

      {field.description && (
        <p className="text-gray-600 text-sm mb-2">{field.description}</p>
      )}

      {field.default !== null && !isEnum && (
        <div className="text-sm text-gray-500">
          Default: <code className="bg-gray-100 px-2 py-0.5 rounded">{
            typeof field.default === 'object' ? JSON.stringify(field.default) : String(field.default)
          }</code>
        </div>
      )}
    </div>
  )
}

function generateExampleJSON(model: Model): string {
  const example: Record<string, any> = {}

  Object.entries(model.fields).forEach(([fieldName, field]) => {
    if (field.default !== null && !field.required) {
      return // Skip optional fields with defaults
    }

    // Generate example values based on type
    // Use word boundary regex to avoid false positives (e.g., 'Candidate' should not match 'date')
    const type = field.type.toLowerCase()

    // Check container types first to avoid matching inner types (e.g., list[str] should not match 'str')
    if (/\b(list|array)\b/.test(type)) {
      example[fieldName] = []
    } else if (/\b(dict|mapping|record)\b/.test(type)) {
      example[fieldName] = {}
    } else if (/\bdate/.test(type)) {
      // Match 'date', 'datetime', 'Date', etc. but not 'Candidate'
      example[fieldName] = '2024-01-01'
    } else if (/\bint\b/.test(type)) {
      example[fieldName] = 42
    } else if (/\b(float|decimal)\b/.test(type)) {
      example[fieldName] = 3.14
    } else if (/\bbool\b/.test(type)) {
      example[fieldName] = true
    } else if (/\bstr\b/.test(type)) {
      // Don't wrap in quotes - JSON.stringify handles string quoting
      example[fieldName] = field.description
        ? field.description.slice(0, 30) + (field.description.length > 30 ? '...' : '')
        : `example_${fieldName}`
    } else {
      example[fieldName] = null
    }
  })

  return JSON.stringify(example, null, 2)
}
