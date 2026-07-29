/**
 * Phase 11 T11-3 — animated loading skeleton.
 *
 * Renders `lines` placeholder blocks that pulse to indicate content is
 * loading. Used by views to show a placeholder while TanStack Query is
 * fetching (alternative to a blank space or a spinner — skeleting reads
 * as "data is coming, here's the shape").
 *
 * Default 3 lines, dark-theme background matching the App shell.
 */
import type { CSSProperties } from "react";

interface Props {
  lines?: number;
  "data-testid"?: string;
}

export function Skeleton({ lines = 3, ...rest }: Props): JSX.Element {
  const style: CSSProperties = {
    display: "flex",
    flexDirection: "column",
    gap: "0.5rem",
    padding: "1rem",
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: "6px",
  };
  return (
    <div data-testid={rest["data-testid"]} style={style}>
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          style={{
            height: "0.875rem",
            background: "#21262d",
            borderRadius: "3px",
            width: `${60 + ((i * 13) % 35)}%`,
            animation: "ekrs-skeleton-pulse 1.4s ease-in-out infinite",
          }}
        />
      ))}
    </div>
  );
}
