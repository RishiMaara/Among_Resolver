// Read images with Tesseract.js, with the settings the app uses in the
// browser (src/lib/scan-ocr.ts): English, one uniform block (PSM 6). Prints
// one JSON object per line: { file, text, confidence }.
//
//   node scripts/tesseract_read.mjs page1.png page2.png ...
//
// Used by engine/scripts/ocr_eval.py to measure the browser's OCR path.
import { tmpdir } from "node:os";
import { createWorker, PSM } from "tesseract.js";

// Node caches the downloaded language data in the working directory by
// default, which dropped an eng.traineddata into the repository.
const worker = await createWorker("eng", undefined, { cachePath: tmpdir() });
await worker.setParameters({ tessedit_pageseg_mode: PSM.SINGLE_BLOCK });
for (const file of process.argv.slice(2)) {
  const { data } = await worker.recognize(file);
  process.stdout.write(
    JSON.stringify({ file, text: data.text, confidence: data.confidence }) + "\n",
  );
}
await worker.terminate();
