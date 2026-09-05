/**
 * The demo, in one click.
 *
 * Seeing anything at all used to require typing a batch id, a net amount, a
 * settlement date and a deductions figure, then attaching three files. That is
 * four chances to mistype in front of an audience, and the settlement date is
 * the cruel one: leaving it at today looks completely reasonable and silently
 * produces "0 candidates within the 5-day window", because the fixtures are
 * dated 2026-09-02. The engine is behaving correctly and the demo looks broken.
 *
 * The values below are the ones sample-data/README.md documents, and
 * engine/tests/test_sample_walkthrough.py asserts that a run with exactly these
 * inputs produces exactly the output that page claims. So this file, that page
 * and the test cannot drift from each other without the suite failing.
 */

export interface SamplePreset {
  label: string;
  description: string;
  batchId: string;
  netAmount: string;
  settledAt: string;
  windowDays: string;
  memberSource: string;
  declaredDeductions: string;
  /** What the engine returns for these inputs, so the UI can say so up front. */
  expected: string;
}

/** The fixtures are served from public/sample-data so the browser can fetch them. */
const FILES = {
  gateway: "/sample-data/gateway_report.csv",
  bank: "/sample-data/bank_statement.csv",
  erp: "/sample-data/erp_ledger.json",
} as const;

/**
 * Both outcomes, because showing only the cleared one sells the wrong thing.
 * The withheld run is the one that demonstrates the actual thesis, and it
 * differs from the cleared run by a single dropdown.
 */
export const SAMPLE_PRESETS: SamplePreset[] = [
  {
    label: "Sample: clears",
    description: "14 gateway payments compose the settlement, ties out to the paisa.",
    batchId: "SETTLE-001",
    netAmount: "66466.36",
    // 16 chars: the datetime-local format the form binds to.
    settledAt: "2026-09-02T00:00",
    windowDays: "5",
    memberSource: "gateway",
    declaredDeductions: "2055.66",
    expected: "cleared · confidence 0.95 · 14 matched · residual 0",
  },
  {
    label: "Sample: withholds",
    description:
      "The same settlement with the member feed undeclared. Every gateway payment " +
      "has an ERP twin at the same amount, so nothing distinguishes them and the " +
      "engine declines rather than guessing.",
    batchId: "SETTLE-001",
    netAmount: "66466.36",
    settledAt: "2026-09-02T00:00",
    windowDays: "5",
    // The form's "Unknown" option is the empty string, and the request omits
    // member_source entirely when it is blank. Sending the literal "unknown"
    // is a 422 from the engine, which is what this preset did until it was
    // clicked in a browser rather than reasoned about.
    memberSource: "",
    declaredDeductions: "2055.66",
    expected: "withheld · confidence 0.54 · 13 matched · needs review",
  },
];

/**
 * Fetch the three fixtures and hand back real File objects.
 *
 * File rather than Blob because the form sends them through FormData to the
 * same endpoint a human upload uses — the point of the preset is to skip the
 * typing, not to take a different code path than a judge would.
 */
export async function loadSampleFiles(): Promise<{
  gateway: File;
  bank: File;
  erp: File;
}> {
  const grab = async (url: string, name: string, type: string) => {
    const res = await fetch(url);
    if (!res.ok) {
      throw new Error(
        `Could not load ${name} (${res.status}). The sample fixtures live in ` +
          `public/sample-data/; they are missing from this build.`,
      );
    }
    return new File([await res.blob()], name, { type });
  };

  const [gateway, bank, erp] = await Promise.all([
    grab(FILES.gateway, "gateway_report.csv", "text/csv"),
    grab(FILES.bank, "bank_statement.csv", "text/csv"),
    grab(FILES.erp, "erp_ledger.json", "application/json"),
  ]);

  return { gateway, bank, erp };
}
