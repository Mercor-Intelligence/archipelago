import { Persona } from '@/lib/api-config';

interface PersonasTableProps {
  personas: Persona[];
}

export default function PersonasTable({ personas }: PersonasTableProps) {
  if (personas.length === 0) return null;

  return (
    <div className="mb-6">
      <h3 className="text-sm font-semibold text-gray-900 uppercase tracking-wide mb-3">Available Personas</h3>
      <div className="overflow-x-auto border border-gray-200 rounded-lg">
        <table className="min-w-full divide-y divide-gray-200">
          <thead className="bg-gray-50">
            <tr>
              <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 uppercase">Username</th>
              <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 uppercase">Password</th>
              <th className="px-4 py-2 text-left text-xs font-semibold text-gray-600 uppercase">Description</th>
            </tr>
          </thead>
          <tbody className="bg-white divide-y divide-gray-200">
            {personas.map((persona, index) => (
              <tr key={index} className="hover:bg-gray-50">
                <td className="px-4 py-2 text-sm font-mono text-indigo-600">{persona.username}</td>
                <td className="px-4 py-2 text-sm font-mono text-gray-600">{persona.password}</td>
                <td className="px-4 py-2 text-sm text-gray-700">{persona.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
