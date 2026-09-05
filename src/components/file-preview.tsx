/**
 * Look at the file you just attached.
 *
 * The dropzone showed a name and a byte count, which answers "did something
 * upload" and not "did the right thing upload". Those are different
 * questions, and on a reconciliation tool the second one matters: the whole
 * failure mode this engine exists to prevent starts with the wrong export
 * being handed to it. A name is a poor way to tell yesterday's gateway file
 * from today's.
 *
 * Everything here is client-side. The file is read in the browser and never
 * sent anywhere to be previewed — a preview that uploaded the file to look at
 * it would be a worse trade than not having one.
 */

import { useEffect, useMemo, useState } from "react";

/** How many rows to render. A 50,000-row gateway export would lock the tab. */
const ROW_LIMIT = 200;
/** How much of the file to read at all. Reading 200 MB to show 200 rows is waste. */
const READ_LIMIT_BYTES = 4 * 1024 * 1024;

/**
 * RFC-4180 enough: quoted fields, embedded commas and newlines, doubled
 * quotes. A naive split(",") mangles exactly the files this app produces —
 * the queue's own CSV export quotes reasoning strings that contain commas —
 * so the preview would misrepresent a file the tool itself wrote.
 */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;

  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          quoted = false;
        }
      } else {
        field += c;
      }
      continue;
    }
    if (c === '"') {
      quoted = true;
    } else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else if (c !== "\r") {
      field += c;
    }
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => r.some((cell) => cell.trim() !== ""));
}

interface Parsed {
  headers: string[];
  rows: string[][];
  totalRows: number;
  truncatedRead: boolean;
  kind: "csv" | "json";
  note: string | null;
}

function parseJson(text: string): Parsed {
  const data = JSON.parse(text);
  const list: unknown[] = Array.isArray(data)
    ? data
    : (data?.settlements ?? data?.transactions ?? data?.records ?? []);
  if (!Array.isArray(list) || list.length === 0) {
    return {
      headers: [],
      rows: [],
      totalRows: 0,
      truncatedRead: false,
      kind: "json",
      note: "No array of records found in this JSON.",
    };
  }
  const headers = Array.from(
    list.slice(0, 50).reduce<Set<string>>((acc, item) => {
      if (item && typeof item === "object") {
        Object.keys(item as object).forEach((k) => acc.add(k));
      }
      return acc;
    }, new Set<string>()),
  );
  const rows = list.slice(0, ROW_LIMIT).map((item) =>
    headers.map((h) => {
      const v = (item as Record<string, unknown>)?.[h];
      return v == null ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
    }),
  );
  return {
    headers,
    rows,
    totalRows: list.length,
    truncatedRead: false,
    kind: "json",
    note: null,
  };
}

export function FilePreview({ file, onClose }: { file: File; onClose: () => void }) {
  const [parsed, setParsed] = useState<Parsed | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const truncatedRead = file.size > READ_LIMIT_BYTES;
        const slice = truncatedRead ? file.slice(0, READ_LIMIT_BYTES) : file;
        const text = await slice.text();

        if (file.name.toLowerCase().endsWith(".json")) {
          const p = parseJson(text);
          if (live) setParsed({ ...p, truncatedRead });
          return;
        }

        const all = parseCsv(text);
        if (all.length === 0) {
          if (live) setError("This file has no readable rows.");
          return;
        }
        // A truncated read almost certainly cut the last row in half, so it
        // is dropped rather than shown as though the data were malformed.
        const body = truncatedRead ? all.slice(1, -1) : all.slice(1);
        if (live) {
          setParsed({
            headers: all[0] ?? [],
            rows: body.slice(0, ROW_LIMIT),
            totalRows: body.length,
            truncatedRead,
            kind: "csv",
            note: null,
          });
        }
      } catch (e) {
        if (live) {
          setError(e instanceof Error ? e.message : "This file could not be read.");
        }
      }
    })();
    return () => {
      live = false;
    };
  }, [file]);

  // Escape closes. A modal that traps a reviewer is worse than no modal.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const sizeLabel = useMemo(() => {
    const b = file.size;
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    return `${(b / (1024 * 1024)).toFixed(1)} MB`;
  }, [file.size]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: "color-mix(in oklab, var(--foreground) 45%, transparent)" }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={`Preview of ${file.name}`}
    >
      <div
        className="flex max-h-[82vh] w-full max-w-[1000px] flex-col overflow-hidden rounded-[16px] border border-border bg-card"
        onClick={(e) => e.stopPropagation()}
        style={{ animation: "rb-rise 240ms ease both" }}
      >
        <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-3.5">
          <div className="min-w-0">
            <p className="m-0 truncate text-[14px] font-semibold">{file.name}</p>
            <p className="m-0 mt-0.5 font-mono text-[11px] text-muted-foreground">
              {sizeLabel}
              {parsed && ` · ${parsed.totalRows.toLocaleString("en-IN")} rows`}
              {parsed && ` · ${parsed.headers.length} columns`}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close preview"
            className="size-7 flex-none rounded-md text-[15px] text-muted-foreground hover:text-foreground"
          >
            ✕
          </button>
        </div>

        {error && (
          <div className="px-5 py-10 text-center">
            <p className="m-0 text-[13px] text-destructive">{error}</p>
          </div>
        )}

        {!parsed && !error && (
          <div className="px-5 py-10 text-center">
            <p className="m-0 text-[13px] text-muted-foreground">Reading file…</p>
          </div>
        )}

        {parsed && parsed.headers.length > 0 && (
          <div className="min-h-0 flex-1 overflow-auto">
            <table className="w-full border-collapse text-left">
              <thead className="sticky top-0 bg-card">
                <tr className="border-b border-border">
                  <th className="label-ui px-2.5 py-2 text-[9.5px] uppercase text-muted-foreground">
                    #
                  </th>
                  {parsed.headers.map((h, i) => (
                    <th
                      key={`${h}-${i}`}
                      className="label-ui whitespace-nowrap px-2.5 py-2 text-[9.5px] uppercase text-muted-foreground"
                    >
                      {h || <span className="opacity-50">(unnamed)</span>}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {parsed.rows.map((r, i) => (
                  <tr key={i} className="border-b border-border last:border-0">
                    <td className="px-2.5 py-1.5 font-mono text-[10.5px] tabular-nums text-muted-foreground">
                      {i + 1}
                    </td>
                    {parsed.headers.map((_, c) => (
                      <td
                        key={c}
                        className="max-w-[280px] truncate px-2.5 py-1.5 font-mono text-[11.5px]"
                        title={r[c] ?? ""}
                      >
                        {r[c] ?? ""}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {parsed && (
          <div className="border-t border-border px-5 py-2.5">
            <p className="m-0 text-[11.5px] text-muted-foreground">
              {parsed.note ??
                (parsed.totalRows > parsed.rows.length
                  ? `Showing the first ${parsed.rows.length} of ${parsed.totalRows.toLocaleString("en-IN")} rows.`
                  : `All ${parsed.totalRows} rows shown.`)}
              {parsed.truncatedRead &&
                " Only the first 4 MB of this file was read, so the row count is of that much, not the whole file."}{" "}
              Read in your browser — nothing was uploaded to preview it.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
