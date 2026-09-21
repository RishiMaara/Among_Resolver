/**
 * Every figure the judge page shows, with the file it came from.
 *
 * A number on a page is a claim. Each one here names the committed benchmark
 * file and the path inside it that produced it, and measured.test.ts reads
 * those files and fails if a figure here has drifted from its source — the
 * same rule the README's test counts are held to, for the same reason: a
 * figure typed by hand is true only until the next measurement.
 */

export interface Measured {
  label: string;
  before?: string;
  after: string;
  note: string;
  /** engine/docs/benchmarks/<file>, and the dotted path to the figure(s). */
  source: { file: string; path: string; beforePath?: string; scale?: number; digits?: number };
}

export const MEASURED: Measured[] = [
  {
    label: "Linkage vs arithmetic alone (own benchmark, 120 scenarios)",
    before: "0%",
    after: "65%",
    note: "true sets identified; 0 wrong approvals in both arms. Deterministic, not AI.",
    source: {
      file: "baseline_comparison.json",
      beforePath: "baseline_no_linkage.truth_identified_pct",
      path: "with_linkage.truth_identified_pct",
      digits: 0,
    },
  },
  {
    label: "Learned linkage, references stripped (ReconRiver, third-party)",
    before: "24.32%",
    after: "56.76%",
    note: "exact sets found; cycle learned only from other scenarios; 0 false clears.",
    source: {
      file: "learned_linkage.json",
      beforePath: "stripped.before.exact_set_identified_pct",
      path: "stripped.with_history.exact_set_identified_pct",
      digits: 2,
    },
  },
  {
    label: "Reading bank narrations, formats the rules never saw",
    before: "82.71%",
    after: "97.50%",
    note: "rules vs grounded model-first; no value kept unless it is in the narration.",
    source: {
      file: "narration_eval.json",
      beforePath: "regex.held_out.mean_accuracy",
      path: "model_then_regex.held_out.mean_accuracy",
      scale: 100,
      digits: 2,
    },
  },
  {
    label: "Withheld settlements: right, verified proposals reaching a reviewer (of 58)",
    before: "2",
    after: "16",
    note: "fixed rules vs the investigator; ids aliased and label-bearing text removed.",
    source: {
      file: "investigation_eval.json",
      beforePath: "rules.right_and_reaching_reviewer",
      path: "model.right_and_reaching_reviewer",
      digits: 0,
    },
  },
  {
    label: "Confidence calibration error, out-of-sample",
    before: "0.1036",
    after: "0.0397",
    note: "ECE raw vs calibrated; the auto-clear gate still reads the raw figure.",
    source: {
      file: "calibration_fit.json",
      beforePath: "cross.0.ece_raw",
      path: "cross.0.ece_calibrated",
      digits: 4,
    },
  },
  {
    label: "Settlement Q&A: fact questions answered from the record (of 29)",
    after: "29",
    note: "and 15 of 15 declined where the record cannot answer — a small set, not proof.",
    source: { file: "qa_eval.json", path: "fact.right_and_shown", digits: 0 },
  },
  {
    label: "One settlement inside 200,000 records",
    after: "55",
    note: "members found exactly — precision and recall 1.0, no exceptions.",
    source: { file: "scale_proof.json", path: "matched_count", digits: 0 },
  },
];

/** A figure as the page prints it, from a raw value and the source's format. */
export function format(value: number, source: Measured["source"]): string {
  const v = value * (source.scale ?? 1);
  const s = v.toFixed(source.digits ?? 0);
  return source.scale === 100 || source.path.endsWith("_pct") ? `${s}%` : s;
}
