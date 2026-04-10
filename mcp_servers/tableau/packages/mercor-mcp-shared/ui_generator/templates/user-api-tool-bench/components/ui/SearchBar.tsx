// Search input component
interface SearchBarProps {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  size?: 'small' | 'large';
}

export default function SearchBar({ value, onChange, placeholder, size = 'small' }: SearchBarProps) {
  const inputClass = size === 'large'
    ? 'block w-full rounded-lg border border-gray-300 bg-white py-3 pl-10 pr-3 text-base placeholder-gray-500 shadow-sm focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600'
    : 'block w-full rounded-lg border border-gray-300 bg-white py-2 pl-10 pr-3 text-sm placeholder-gray-500 shadow-sm focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-600';

  return (
    <div className="relative">
      <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3">
        <svg className="h-5 w-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
      </div>
      <input
        type="text"
        className={inputClass}
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  );
}

export type { SearchBarProps };
