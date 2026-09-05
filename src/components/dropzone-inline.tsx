/**
 * The design's inline dropzone — a compact card that lives in the Data Sources
 * strip rather than a full-width panel.
 *
 * Ported from design/Agent Flow.dc.html. Drag state, the filled state with a
 * clear button, and the per-file error line all come from there; only the
 * template syntax changed.
 */

import { useRef, useState } from "react";
import { FilePreview } from "@/components/file-preview";

interface Props {
  label: string;
  requirement: string;
  file: File | null;
  onPick: (f: File | null) => void;
  error?: string | null;
  disabled?: boolean;
}

function humanSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function DropzoneInline({ label, requirement, file, onPick, error, disabled }: Props) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [over, setOver] = useState(false);
  const [preview, setPreview] = useState(false);

  const borderColor = error
    ? "var(--destructive)"
    : over
      ? "var(--accent)"
      : file
        ? "color-mix(in oklab, var(--accent) 40%, var(--border))"
        : "var(--border)";

  return (
    <div>
      <div
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          if (disabled) return;
          const f = e.dataTransfer.files?.[0];
          if (f) onPick(f);
        }}
        style={{
          minHeight: 96,
          borderRadius: 11,
          border: `1px dashed ${borderColor}`,
          background: over ? "color-mix(in oklab, var(--accent) 6%, transparent)" : "transparent",
          transition: "border-color 220ms ease, background 220ms ease",
        }}
        className="flex flex-col items-center justify-center p-3 text-center"
      >
        {file ? (
          <div className="flex w-full items-center justify-between gap-2 rounded-[9px] bg-muted/60 px-2.5 py-2.5">
            <button
              type="button"
              onClick={() => setPreview(true)}
              title={`View ${file.name}`}
              className="flex flex-1 items-center gap-2.5 overflow-hidden text-left"
            >
              <span
                className="flex size-5 flex-none items-center justify-center rounded-[5px] text-[11px]"
                style={{
                  background: "color-mix(in oklab, var(--accent) 16%, transparent)",
                  color: "var(--accent)",
                }}
              >
                ↑
              </span>
              <div className="overflow-hidden text-left">
                <p className="truncate text-[12.5px] font-medium underline decoration-dotted decoration-from-font underline-offset-2">
                  {file.name}
                </p>
                <p className="mt-0.5 font-mono text-[10.5px] text-muted-foreground">
                  {humanSize(file.size)} · view
                </p>
              </div>
            </button>
            <button
              type="button"
              onClick={() => onPick(null)}
              disabled={disabled}
              aria-label={`Remove ${file.name}`}
              className="size-6 flex-none rounded-md text-[13px] text-muted-foreground hover:text-foreground disabled:opacity-40"
            >
              ✕
            </button>
          </div>
        ) : (
          <div className="flex flex-col items-center">
            <span
              className="flex size-8 items-center justify-center rounded-full text-[14px]"
              style={{
                background: over
                  ? "color-mix(in oklab, var(--accent) 18%, transparent)"
                  : "var(--muted)",
                color: over ? "var(--accent)" : "var(--muted-foreground)",
                transition: "background 220ms ease, color 220ms ease",
              }}
            >
              ⤒
            </span>
            <p className="mt-2.5 whitespace-nowrap text-[12.5px] font-medium leading-tight">
              {label}
            </p>
            <p className="mt-1 whitespace-nowrap font-mono text-[9.5px] uppercase leading-snug tracking-[0.06em] text-muted-foreground">
              {requirement}
            </p>
            <button
              type="button"
              onClick={() => inputRef.current?.click()}
              disabled={disabled}
              className="mt-2.5 rounded-lg border border-border px-3 py-[5px] text-[12px] disabled:opacity-40"
            >
              Browse
            </button>
          </div>
        )}
        <input
          ref={inputRef}
          type="file"
          accept=".csv,.json"
          className="hidden"
          onChange={(e) => onPick(e.target.files?.[0] ?? null)}
        />
      </div>
      {preview && file && <FilePreview file={file} onClose={() => setPreview(false)} />}
      {error && (
        <p
          className="mt-1.5 flex gap-1.5 text-[12px] leading-snug text-destructive"
          style={{ animation: "af-rise 200ms ease both" }}
        >
          <span>⚠</span>
          <span>{error}</span>
        </p>
      )}
    </div>
  );
}
