import jsPDF from 'jspdf';

function safeStringify(obj: unknown): string {
  try {
    return JSON.stringify(obj);
  } catch {
    return String(obj);
  }
}

// Global MCP activity log store
export interface McpLogEntry {
  id: string;
  timestamp: Date;
  tool: string;
  parameters: Record<string, any>;
  status: 'pending' | 'success' | 'error';
  duration?: number;
  error?: string;
  response?: any;
}

// Entry format for PDF export (can come from client-side log or server-side trajectory)
export interface PdfExportEntry {
  tool: string;
  parameters: Record<string, any>;
  response: any;
  status: 'success' | 'error';
  timestamp: string;
  duration?: number;
}

type LogListener = (entries: McpLogEntry[]) => void;

// Tool labels derived from app's dataTypes config
let TOOL_LABELS: Record<string, { label: string; description: string }> = {};
let _labelsInitialized = false;

/** Initialize tool labels from the app's dataTypes array (from api-config.ts).
 *  Called automatically by McpLogPanel; apps don't need to call this manually. */
export function initToolLabels(dataTypes: Array<{ id: string; name: string; displayName?: string; description: string }>) {
  if (_labelsInitialized) return;
  _labelsInitialized = true;
  for (const dt of dataTypes) {
    TOOL_LABELS[dt.id] = {
      label: dt.displayName || dt.name,
      description: dt.description,
    };
  }
}

// Helper to get human-readable tool label
function getToolLabel(toolName: string): string {
  const info = TOOL_LABELS[toolName];
  if (info) {
    return info.label;
  }
  // Convert snake_case to Title Case: "list_dashboards" -> "List Dashboards"
  return toolName.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

class McpLogStore {
  private entries: McpLogEntry[] = [];
  private listeners: Set<LogListener> = new Set();
  private maxEntries = 100;
  private recentCalls: Map<string, number> = new Map();
  private dedupeWindowMs = 100;

  // Tools to hide from the log (internal/background tools)
  private hiddenTools = new Set<string>();

  addEntry(tool: string, parameters: Record<string, any>): string | null {
    // Skip hidden tools - don't log them at all
    if (this.hiddenTools.has(tool)) {
      return null;
    }

    // De-duplicate calls that happen within a short window (React Strict Mode fix)
    const callKey = `${tool}:${safeStringify(parameters)}`;
    const now = Date.now();
    const lastCall = this.recentCalls.get(callKey);

    if (lastCall && now - lastCall < this.dedupeWindowMs) {
      const existingEntry = this.entries.find(e =>
        e.tool === tool &&
        safeStringify(e.parameters) === safeStringify(parameters) &&
        now - e.timestamp.getTime() < this.dedupeWindowMs
      );
      if (existingEntry) {
        return existingEntry.id;
      }
    }

    this.recentCalls.set(callKey, now);

    // Clean up old entries from recentCalls map
    if (this.recentCalls.size > 50) {
      const cutoff = now - 1000;
      Array.from(this.recentCalls.entries()).forEach(([key, time]) => {
        if (time < cutoff) {
          this.recentCalls.delete(key);
        }
      });
    }

    const id = `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    const entry: McpLogEntry = {
      id,
      timestamp: new Date(),
      tool,
      parameters,
      status: 'pending',
    };

    this.entries = [entry, ...this.entries].slice(0, this.maxEntries);
    this.notifyListeners();
    return id;
  }

  updateEntry(id: string | null, updates: Partial<McpLogEntry>) {
    if (!id) return;
    // Truncate large responses to prevent memory issues (limit ~500KB)
    if (updates.response) {
      updates.response = this.truncateLargeResponse(updates.response);
    }
    this.entries = this.entries.map(entry =>
      entry.id === id ? { ...entry, ...updates } : entry
    );
    this.notifyListeners();
  }

  private truncateLargeResponse(response: any, maxSize: number = 500000): any {
    const jsonStr = safeStringify(response);
    if (jsonStr.length <= maxSize) {
      return response;
    }

    const truncated = this.truncateObject(response, maxSize);
    return {
      ...truncated,
      _truncated: true,
      _originalSize: `${Math.round(jsonStr.length / 1024)}KB`
    };
  }

  private truncateObject(obj: any, maxSize: number): any {
    if (typeof obj === 'string') {
      if (obj.length > 1000) {
        if (/^[A-Za-z0-9+/=]+$/.test(obj)) {
          return `[base64 data - ${Math.round(obj.length / 1024)}KB]`;
        }
        return obj.slice(0, 1000) + `... [truncated ${obj.length - 1000} chars]`;
      }
      return obj;
    }

    if (Array.isArray(obj)) {
      if (obj.length > 100) {
        return [
          ...obj.slice(0, 100).map(item => this.truncateObject(item, maxSize)),
          { _truncated: true, _remaining: obj.length - 100 }
        ];
      }
      return obj.map(item => this.truncateObject(item, maxSize));
    }

    if (obj && typeof obj === 'object') {
      const result: Record<string, any> = {};
      for (const [key, value] of Object.entries(obj)) {
        result[key] = this.truncateObject(value, maxSize);
      }
      return result;
    }

    return obj;
  }

  getEntries(): McpLogEntry[] {
    return this.entries;
  }

  clearEntries() {
    this.entries = [];
    this.notifyListeners();
  }

  subscribe(listener: LogListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private notifyListeners() {
    this.listeners.forEach(listener => listener(this.entries));
  }
}

// Singleton instance
export const mcpLog = new McpLogStore();

// ── Standalone PDF export ──────────────────────────────────────────────
// Can be fed data from either the client-side activity log or the
// server-side trajectory recorder.

// Format response data for PDF output
function formatResponseForPdf(response: any): string[] {
  const lines: string[] = [];

  if (!response) {
    lines.push('(empty response)');
    return lines;
  }

  const formatValue = (value: any, indent: number, maxItems: number = 5): void => {
    const prefix = '  '.repeat(indent);

    if (value === null || value === undefined) {
      return;
    }

    if (Array.isArray(value)) {
      if (value.length === 0) {
        return;
      }

      const firstItem = value[0];
      if (typeof firstItem === 'object' && firstItem !== null) {
        value.slice(0, maxItems).forEach((item, idx) => {
          const label = item.name || item.title || item.id || `[${idx}]`;
          const extraInfo = [];
          if (item.id && item.name !== item.id && item.title !== item.id) extraInfo.push(`id: ${item.id}`);
          if (item.type) extraInfo.push(`type: ${item.type}`);
          if (item.label && item.label !== item.name) extraInfo.push(`label: ${item.label}`);

          const suffix = extraInfo.length > 0 ? ` (${extraInfo.join(', ')})` : '';
          lines.push(`${prefix}- ${label}${suffix}`);
        });
        if (value.length > maxItems) {
          lines.push(`${prefix}... and ${value.length - maxItems} more`);
        }
      } else {
        if (value.length <= 10 && value.join(', ').length < 80) {
          lines.push(`${prefix}${value.join(', ')}`);
        } else {
          value.slice(0, maxItems).forEach(item => {
            lines.push(`${prefix}- ${item}`);
          });
          if (value.length > maxItems) {
            lines.push(`${prefix}... and ${value.length - maxItems} more`);
          }
        }
      }
    } else if (typeof value === 'object') {
      formatObject(value, indent, maxItems);
    }
  };

  const formatObject = (obj: any, indent: number, maxItems: number = 5): void => {
    const prefix = '  '.repeat(indent);
    const keys = Object.keys(obj);

    keys.forEach(key => {
      const val = obj[key];

      if (val === null || val === undefined) {
        return;
      }

      if (typeof val === 'string' && val.length > 500 && /^[A-Za-z0-9+/=]+$/.test(val)) {
        lines.push(`${prefix}${key}: [base64 data - ${Math.round(val.length / 1024)}KB]`);
        return;
      }

      if (Array.isArray(val)) {
        if (val.length === 0) {
          lines.push(`${prefix}${key}: []`);
        } else {
          lines.push(`${prefix}${key}: [${val.length} items]`);
          formatValue(val, indent + 1, maxItems);
        }
      } else if (typeof val === 'object') {
        const objKeys = Object.keys(val);
        if (objKeys.length === 0) {
          lines.push(`${prefix}${key}: {}`);
        } else if (objKeys.length <= 4) {
          const simple = objKeys.every(k => typeof val[k] !== 'object' || val[k] === null);
          if (simple) {
            const pairs = objKeys.map(k => `${k}: ${val[k]}`).join(', ');
            if (pairs.length < 60) {
              lines.push(`${prefix}${key}: {${pairs}}`);
            } else {
              lines.push(`${prefix}${key}:`);
              formatObject(val, indent + 1, maxItems);
            }
          } else {
            lines.push(`${prefix}${key}:`);
            formatObject(val, indent + 1, maxItems);
          }
        } else {
          lines.push(`${prefix}${key}:`);
          formatValue(val, indent + 1, maxItems);
        }
      } else if (typeof val === 'string' && val.length > 100) {
        lines.push(`${prefix}${key}: ${val.slice(0, 100)}...`);
      } else {
        lines.push(`${prefix}${key}: ${val}`);
      }
    });
  };

  formatObject(response, 1);

  return lines;
}

/** Generate and download a PDF activity report from a list of entries. */
export function generateActivityPdf(entries: PdfExportEntry[], sessionName: string): void {
  if (entries.length === 0) {
    console.warn('No activity to export');
    return;
  }

  const doc = new jsPDF({
    orientation: 'portrait',
    unit: 'mm',
    format: 'a4',
  });

  const pageWidth = doc.internal.pageSize.getWidth();
  const pageHeight = doc.internal.pageSize.getHeight();
  const margin = 15;
  const contentWidth = pageWidth - margin * 2;
  const lineHeight = 4;
  let y = margin;

  const checkNewPage = (neededHeight: number) => {
    if (y + neededHeight > pageHeight - margin) {
      doc.addPage();
      y = margin;
      return true;
    }
    return false;
  };

  // Title
  doc.setFontSize(16);
  doc.setFont('helvetica', 'bold');
  doc.setTextColor(0);
  doc.text(sessionName || 'MCP Activity Log', margin, y);
  y += 7;

  // Metadata
  doc.setFontSize(9);
  doc.setFont('helvetica', 'normal');
  doc.setTextColor(100);
  doc.text(`Generated: ${new Date().toLocaleString()}  |  ${entries.length} tool calls`, margin, y);
  y += 6;

  // Divider line
  doc.setDrawColor(180);
  doc.line(margin, y, pageWidth - margin, y);
  y += 8;

  // Each tool call entry
  entries.forEach((entry, index) => {
    checkNewPage(25);

    const toolLabel = getToolLabel(entry.tool);

    // Step header with background
    doc.setFillColor(240, 240, 240);
    doc.rect(margin, y - 1, contentWidth, 6, 'F');
    doc.setFontSize(10);
    doc.setFont('helvetica', 'bold');
    doc.setTextColor(0);
    doc.text(`Step ${index + 1}: ${toolLabel}`, margin + 2, y + 3);
    y += 8;

    // Tool name
    doc.setFontSize(8);
    doc.setFont('helvetica', 'normal');
    doc.setTextColor(100);
    doc.text('Tool:', margin + 2, y);
    doc.setTextColor(0);
    doc.setFont('courier', 'normal');
    doc.text(entry.tool, margin + 12, y);
    y += 5;

    // Parameters
    const params = Object.entries(entry.parameters);
    if (params.length > 0) {
      doc.setFont('helvetica', 'normal');
      doc.setTextColor(100);
      doc.text('Parameters:', margin + 2, y);
      y += 4;

      doc.setFontSize(8);
      for (const [key, value] of params) {
        checkNewPage(5);
        let displayValue = typeof value === 'object' ? safeStringify(value) : String(value);

        if (displayValue.length > 100 && /^[A-Za-z0-9+/=]+$/.test(displayValue)) {
          displayValue = '[base64 data]';
        }

        doc.setTextColor(80);
        doc.setFont('helvetica', 'normal');
        doc.text(`${key}:`, margin + 6, y);

        const keyWidth = doc.getTextWidth(`${key}: `);
        const valueX = margin + 6 + keyWidth;
        const valueMaxWidth = contentWidth - keyWidth - 8;

        doc.setTextColor(0);
        doc.setFont('courier', 'normal');

        if (doc.getTextWidth(displayValue) > valueMaxWidth) {
          const wrappedLines = doc.splitTextToSize(displayValue, valueMaxWidth);
          doc.text(wrappedLines[0], valueX, y);
          y += lineHeight;
          for (let i = 1; i < Math.min(wrappedLines.length, 3); i++) {
            checkNewPage(lineHeight);
            doc.text(wrappedLines[i], margin + 6, y);
            y += lineHeight;
          }
          if (wrappedLines.length > 3) {
            doc.setTextColor(100);
            doc.text(`... (${wrappedLines.length - 3} more lines)`, margin + 6, y);
            y += lineHeight;
          }
        } else {
          doc.text(displayValue, valueX, y);
          y += lineHeight;
        }
      }
    }
    y += 2;

    // Response section
    checkNewPage(8);
    const status = entry.status;

    doc.setFont('helvetica', 'normal');
    doc.setFontSize(8);
    doc.setTextColor(100);
    doc.text('Response:', margin + 2, y);

    if (status === 'success') {
      doc.setTextColor(34, 139, 34);
    } else {
      doc.setTextColor(220, 38, 38);
    }
    doc.setFont('helvetica', 'bold');
    doc.text(status === 'success' ? 'Success' : 'Error', margin + 20, y);
    y += 5;

    // Response details
    if (entry.response) {
      doc.setFont('helvetica', 'normal');
      doc.setFontSize(8);
      doc.setTextColor(0);

      const responseLines = formatResponseForPdf(entry.response);
      for (const line of responseLines) {
        checkNewPage(lineHeight);

        const indent = line.startsWith('    ') ? 12 : line.startsWith('  ') ? 6 : 4;
        const trimmedLine = line.trimStart();
        const colonIdx = trimmedLine.indexOf(':');

        if (colonIdx > 0 && colonIdx < 20) {
          const key = trimmedLine.slice(0, colonIdx + 1);
          const value = trimmedLine.slice(colonIdx + 1).trim();

          doc.setTextColor(80);
          doc.text(key, margin + indent, y);

          const keyW = doc.getTextWidth(key + ' ');
          const valueMaxW = contentWidth - indent - keyW - 4;

          doc.setTextColor(0);
          if (doc.getTextWidth(value) > valueMaxW && value.length > 0) {
            const wrapped = doc.splitTextToSize(value, valueMaxW);
            doc.text(wrapped[0], margin + indent + keyW, y);
            y += lineHeight;
            for (let i = 1; i < Math.min(wrapped.length, 2); i++) {
              checkNewPage(lineHeight);
              doc.text(wrapped[i], margin + indent + 2, y);
              y += lineHeight;
            }
            if (wrapped.length > 2) {
              doc.setTextColor(100);
              doc.setFontSize(7);
              doc.text(`... truncated`, margin + indent + 2, y);
              doc.setFontSize(8);
              y += lineHeight;
            }
          } else {
            doc.text(value, margin + indent + keyW, y);
            y += lineHeight;
          }
        } else {
          doc.setTextColor(0);
          const maxW = contentWidth - indent - 4;
          if (doc.getTextWidth(trimmedLine) > maxW) {
            const wrapped = doc.splitTextToSize(trimmedLine, maxW);
            for (let i = 0; i < Math.min(wrapped.length, 2); i++) {
              checkNewPage(lineHeight);
              doc.text(wrapped[i], margin + indent, y);
              y += lineHeight;
            }
          } else {
            doc.text(trimmedLine, margin + indent, y);
            y += lineHeight;
          }
        }
      }
    }

    y += 3;

    // Divider
    checkNewPage(6);
    doc.setDrawColor(220);
    doc.line(margin, y, pageWidth - margin, y);
    y += 6;
  });

  // Save the PDF
  const filename = `mcp-activity-log-${new Date().toISOString().slice(0, 10)}.pdf`;
  doc.save(filename);
}
