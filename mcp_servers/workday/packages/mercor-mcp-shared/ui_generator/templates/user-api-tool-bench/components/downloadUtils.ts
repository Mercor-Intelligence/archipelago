// Download utilities for CSV and JSON export
import Papa from 'papaparse';

export interface ExportableArray {
  key: string;
  data: any[];
}

// Find ALL arrays of objects in data (handles nested API responses up to 3 levels deep)
const findAllArrays = (
  data: any,
  depth: number = 0,
  currentKey: string = 'data'
): ExportableArray[] => {
  const results: ExportableArray[] = [];
  if (depth > 3) return results;

  if (Array.isArray(data) && data.length > 0 && typeof data[0] === 'object' && data[0] !== null && !Array.isArray(data[0])) {
    // Found an array of objects - add it to results
    results.push({ key: currentKey, data });
    // Also recurse into each object in the array to find nested arrays
    for (const item of data) {
      if (item && typeof item === 'object') {
        for (const [key, value] of Object.entries(item)) {
          const nested = findAllArrays(value, depth + 1, key);
          results.push(...nested);
        }
      }
    }
  } else if (data && typeof data === 'object' && !Array.isArray(data)) {
    // Regular object - recurse into its values
    for (const [key, value] of Object.entries(data)) {
      const nested = findAllArrays(value, depth + 1, key);
      results.push(...nested);
    }
  }
  return results;
};

// Get all exportable arrays from data
export const getExportableArrays = (data: any): ExportableArray[] => {
  return findAllArrays(data);
};

// Convert a specific array to CSV using papaparse
export const arrayToCsv = (data: any[]): string => {
  return Papa.unparse(data);
};

// Combine multiple arrays into a single CSV with # section_name headers
export const combineArraysToCsv = (arrays: ExportableArray[]): string => {
  if (arrays.length === 0) return '';
  if (arrays.length === 1) return Papa.unparse(arrays[0].data);

  const sections: string[] = [];
  for (const arr of arrays) {
    const sectionHeader = `# ${arr.key}`;
    const csvData = Papa.unparse(arr.data);
    sections.push(`${sectionHeader}\n${csvData}`);
  }
  return sections.join('\n\n');
};

// Trigger file download - uses data URI for iframe compatibility
export const downloadFile = (content: string, filename: string, mimeType: string) => {
  // Use data URI approach for better iframe/sandbox compatibility
  const dataUri = `data:${mimeType};charset=utf-8,${encodeURIComponent(content)}`;
  const a = document.createElement('a');
  a.href = dataUri;
  a.download = filename;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
};

// Download data as JSON file
export const downloadJson = (data: any) => {
  const jsonString = JSON.stringify(data, null, 2);
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  downloadFile(jsonString, `result-${timestamp}.json`, 'application/json');
};

// Download arrays as CSV file
export const downloadCsv = (data: any) => {
  const arrays = getExportableArrays(data);
  if (arrays.length === 0) return;
  const csv = combineArraysToCsv(arrays);
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  downloadFile(csv, `result-${timestamp}.csv`, 'text/csv');
};
