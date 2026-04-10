'use client';

import { useEffect, useRef, useState } from 'react';
import mermaid from 'mermaid';

interface MermaidDiagramProps {
  chart: string;
}

let mermaidInitialized = false;

export default function MermaidDiagram({ chart }: MermaidDiagramProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let isCancelled = false;

    const renderDiagram = async () => {
      // Clear stale state immediately when chart changes
      setError(null);
      setSvg('');

      if (!mermaidInitialized) {
        mermaid.initialize({
          startOnLoad: false,
          theme: 'default',
          securityLevel: 'strict',
          fontFamily: 'ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
          fontSize: 14,
          flowchart: {
            useMaxWidth: true,
            htmlLabels: false,
            curve: 'basis',
          },
        });
        mermaidInitialized = true;
      }

      try {
        const id = `mermaid-${Math.random().toString(36).substr(2, 9)}`;
        const { svg } = await mermaid.render(id, chart);

        // Only update state if this render hasn't been cancelled
        if (!isCancelled) {
          setSvg(svg);
          setError(null);
        }
      } catch (error) {
        console.error('Mermaid rendering error:', error);

        // Only update state if this render hasn't been cancelled
        if (!isCancelled) {
          setError(error instanceof Error ? error.message : 'Failed to render diagram');
        }
      }
    };

    renderDiagram();

    // Cleanup function to cancel stale renders
    return () => {
      isCancelled = true;
    };
  }, [chart]);

  if (error) {
    return (
      <div className="mermaid-diagram my-6 p-4 border border-red-200 bg-red-50 rounded-lg">
        <p className="text-sm text-red-800 font-medium">Failed to render diagram</p>
        <p className="text-xs text-red-600 mt-1">{error}</p>
      </div>
    );
  }

  return (
    <div
      ref={ref}
      className="mermaid-diagram my-6 flex justify-center"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
