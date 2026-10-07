interface Tab {
  id: string;
  label: string;
  badge?: number;
  visible?: boolean;
}

interface TabNavigationProps {
  tabs: Tab[];
  currentTab: string;
  onTabChange: (tabId: string) => void;
}

export default function TabNavigation({ tabs, currentTab, onTabChange }: TabNavigationProps) {
  const visibleTabs = tabs.filter(tab => tab.visible !== false);

  if (visibleTabs.length <= 1) return null;

  return (
    <div className="mb-6 border-b border-gray-200">
      <nav className="-mb-px flex space-x-8">
        {visibleTabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => onTabChange(tab.id)}
            className={`py-4 px-1 border-b-2 font-medium text-sm transition-colors ${
              currentTab === tab.id
                ? 'border-indigo-500 text-indigo-600'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
            }`}
          >
            {tab.label}
            {tab.badge !== undefined && tab.badge > 0 && (
              <span className="ml-2 px-2 py-0.5 text-xs bg-indigo-100 text-indigo-700 rounded-full">
                {tab.badge}
              </span>
            )}
          </button>
        ))}
      </nav>
    </div>
  );
}
