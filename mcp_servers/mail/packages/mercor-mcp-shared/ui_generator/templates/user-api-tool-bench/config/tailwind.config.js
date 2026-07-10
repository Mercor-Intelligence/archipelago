/** @type {import('tailwindcss').Config} */
const path = require('path');
const fs = require('fs');

// Load MCP shared paths from cache
let sharedComponentsPath = null;
try {
  const pathsFile = path.resolve('./node_modules/.cache/mcp-paths.json');
  if (fs.existsSync(pathsFile)) {
    const mcpPaths = JSON.parse(fs.readFileSync(pathsFile, 'utf8'));
    sharedComponentsPath = mcpPaths.components;
  }
} catch (e) {
  // Paths not available yet, will be loaded after resolve-paths runs
}

module.exports = {
  content: [
    './pages/**/*.{js,ts,jsx,tsx,mdx}',
    './components/**/*.{js,ts,jsx,tsx,mdx}',
    './app/**/*.{js,ts,jsx,tsx,mdx}',
    // Include shared components from mercor-mcp-shared
    ...(sharedComponentsPath ? [`${sharedComponentsPath}/**/*.{js,ts,jsx,tsx,mdx}`] : []),
  ],
  theme: {
    extend: {},
  },
  plugins: [],
};
