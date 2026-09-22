/**
 * Read a scanned bank statement in the visitor's browser.
 *
 * Two existing tools, nothing written from scratch: pdf.js (Mozilla) turns a
 * PDF page into an image, and Tesseract.js (the Tesseract OCR engine compiled
 * to WebAssembly) reads the image. Both are Apache-2.0, need no key, cost
 * nothing, and the statement never leaves the machine to be read.
 *
 * The engine does not trust the result. It reads the text with the same
 * parser as a text PDF and uses it only if every line's running balance
 * follows from the one before; a misread digit is refused, with the line
 * named. Measured on the sample scan, in this browser and in Node, three
 * settings decide whether Tesseract reads a statement table correctly, and all
 * three are set here:
 *   - the page is read at 3,400 pixels wide — twice a 150 dpi scan. At 1x it
 *     misread a balance (5 as 6); at 3,000 it did again, rescaling artefacts
 *     and all; at 2,550, 3,400 and 4,250 it read all six right.
 *   - as one uniform block (PSM 6), so each table row stays one line. With
 *     automatic layout it split the table into columns and lost the rows.
 *   - pdf.js renders with the "print" intent, which does not wait for
 *     animation frames: a tab in the background pauses those, and a scan
 *     read while the visitor looked elsewhere never finished.
 *
 * Loaded on demand: neither library ships in the page until a scan appears.
 */

const TARGET_WIDTH = 3400; // pixels the page is read at
const MAX_PAGES = 5;

export const BANK_FILE_TYPES = ".csv,.json,.sta,.mt940,.940,.txt,.xml,.ofx,.pdf,.png,.jpg,.jpeg";

export async function isScan(file: File): Promise<boolean> {
  if (/^image\/(png|jpe?g)$/.test(file.type) || /\.(png|jpe?g)$/i.test(file.name)) return true;
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") return false;
  const task = await openPdf(file);
  try {
    const page = await (await task.promise).getPage(1);
    const content = await page.getTextContent();
    const text = content.items.map((i) => ("str" in i ? i.str : "")).join("");
    return text.trim().length < 20; // the same threshold the engine uses
  } finally {
    void task.destroy();
  }
}

async function openPdf(file: File) {
  const pdfjs = await import("pdfjs-dist");
  // `?url` is Vite's own way to ship a file as a URL, in dev and in the build
  // alike; `new URL(..., import.meta.url)` on a package path hung in dev.
  const worker = await import("pdfjs-dist/build/pdf.worker.min.mjs?url");
  pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
  return pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()) });
}

async function pdfPages(file: File): Promise<HTMLCanvasElement[]> {
  const task = await openPdf(file);
  try {
    const pdf = await task.promise;
    const out: HTMLCanvasElement[] = [];
    for (let n = 1; n <= Math.min(pdf.numPages, MAX_PAGES); n++) {
      const page = await pdf.getPage(n);
      const base = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: TARGET_WIDTH / base.width });
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(viewport.width);
      canvas.height = Math.round(viewport.height);
      await page.render({ canvas, viewport, intent: "print" }).promise;
      out.push(canvas);
    }
    return out;
  } finally {
    void task.destroy();
  }
}

async function imagePage(file: File): Promise<HTMLCanvasElement> {
  const bitmap = await createImageBitmap(file);
  const scale = bitmap.width < TARGET_WIDTH ? TARGET_WIDTH / bitmap.width : 1;
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("This browser cannot draw the image to read it.");
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return canvas;
}

/** The statement's text as Tesseract read it, one table row per line. */
export async function readScan(file: File, onProgress?: (note: string) => void): Promise<string> {
  onProgress?.("Preparing the scan…");
  const pages =
    /\.pdf$/i.test(file.name) || file.type === "application/pdf"
      ? await pdfPages(file)
      : [await imagePage(file)];
  // Tesseract.js is CommonJS; bundled, its API arrives on `default`.
  const mod = await import("tesseract.js");
  const { createWorker, PSM } = mod.default ?? mod;
  onProgress?.("Loading OCR (first time only)…");
  const worker = await createWorker("eng");
  try {
    await worker.setParameters({ tessedit_pageseg_mode: PSM.SINGLE_BLOCK });
    const texts: string[] = [];
    for (const [i, page] of pages.entries()) {
      onProgress?.(`Reading page ${i + 1} of ${pages.length} in your browser…`);
      texts.push((await worker.recognize(page)).data.text);
    }
    return texts.join("\n");
  } finally {
    await worker.terminate();
  }
}
