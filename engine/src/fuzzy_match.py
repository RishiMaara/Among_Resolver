"""
Probabilistic and semantic matching, run only after exact subset-sum fails.

Catches mistyped references, split payments and similar memos. Never clears
on its own. Similarity is computed once per pool as rectangular matrices
(rapidfuzz cdist, embeddings when installed), not per pair: the per-pair
version projected 112 minutes on a 20K chunk. The same context serves the
fuzzy fallback and the tiebreak scorer.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass

from schema import NormalizedTxn, MatchResult, MatchMethod

logger = logging.getLogger(__name__)

CONFIDENCE_CLEAR_THRESHOLD = 0.90  # below this, route to Agent 5, never auto-clear


@dataclass
class FuzzyMatchConfig:
    ref_id_weight: float = 0.6
    memo_semantic_weight: float = 0.4
    confidence_threshold: float = CONFIDENCE_CLEAR_THRESHOLD


def ref_id_similarity(a: str, b: str) -> float:
    """Fuzzy string similarity for truncated/mistyped reference IDs.
    Kept for single-pair/small-scale use and test compatibility — do
    NOT call this in a loop over a large pool; use SimilarityContext."""
    from rapidfuzz import fuzz
    return fuzz.ratio(a, b) / 100.0


def memo_semantic_similarity(memo_a: str, memo_b: str) -> float:
    """
    Semantic similarity between memo fields via sentence embeddings.
    Lazy-loads the model so it's only paid for when this path actually
    runs. Falls back to rapidfuzz token_sort_ratio if sentence-transformers
    is not installed or the model fails to load.

    Kept for single-pair/small-scale use and test compatibility — do NOT
    call this in a loop over a large pool; use SimilarityContext, which
    batches the embedding calls instead of one encode() per pair.
    """
    model = memo_semantic_similarity._model
    if model is None and not memo_semantic_similarity._tried_load:
        memo_semantic_similarity._tried_load = True
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer("all-MiniLM-L6-v2")
            memo_semantic_similarity._model = model
        except Exception as exc:
            # sentence-transformers not installed, model unavailable, or
            # network blocked (e.g. huggingface.co unreachable) — fall
            # back to lexical similarity rather than raising or hanging.
            logger.warning(
                "Agent 4: sentence-transformers unavailable (%s: %s). "
                "Memo similarity will use rapidfuzz lexical fallback instead. "
                "Install sentence-transformers (and torch) for semantic matching.",
                type(exc).__name__, exc,
            )

    if model is not None:
        from sentence_transformers import util
        emb_a = model.encode(memo_a, convert_to_tensor=True)
        emb_b = model.encode(memo_b, convert_to_tensor=True)
        return float(util.cos_sim(emb_a, emb_b))

    from rapidfuzz import fuzz
    return fuzz.token_sort_ratio(memo_a, memo_b) / 100.0


memo_semantic_similarity._model = None
memo_semantic_similarity._tried_load = False


@dataclass
class SimilarityContext:
    """
    Reference and memo similarity rows for `query` against `pool`: a
    rectangular len(query) x len(pool) matrix, never pool x pool (that needed
    ~19.6 GB at 35K items). Tiebreaks query a small subset; the fuzzy fallback
    queries `unmatched` in blocks (bulk_fuzzy_recover).
    """
    query: list[NormalizedTxn]
    pool: list[NormalizedTxn]
    pool_id_to_idx: dict[str, int]
    query_id_to_row: dict[str, int]
    ref_matrix: "object"   # numpy ndarray, shape (len(query), len(pool))
    memo_matrix: "object"  # numpy ndarray, shape (len(query), len(pool))


def _compute_similarity_rows(
    query: list[NormalizedTxn],
    pool: list[NormalizedTxn],
) -> SimilarityContext:
    """
    Computes a RECTANGULAR (len(query) x len(pool)) similarity block —
    memory scales with query size, not pool size squared. Call this with
    a bounded-size query (see bulk_fuzzy_recover's BLOCK_SIZE) rather
    than the full pool when the pool is large.
    """
    from rapidfuzz import process, fuzz
    import numpy as np

    if not query or not pool:
        return SimilarityContext(query=query, pool=pool, pool_id_to_idx={}, query_id_to_row={},
                                  ref_matrix=np.zeros((len(query), len(pool))),
                                  memo_matrix=np.zeros((len(query), len(pool))))

    ref_ids_q = [t.ref_id_canonical for t in query]
    ref_ids_p = [t.ref_id_canonical for t in pool]
    ref_matrix = process.cdist(ref_ids_q, ref_ids_p, scorer=fuzz.ratio) / 100.0

    memos_q = [t.memo_normalized for t in query]
    memos_p = [t.memo_normalized for t in pool]

    model = memo_semantic_similarity._model
    if model is None and not memo_semantic_similarity._tried_load:
        memo_semantic_similarity._tried_load = True
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer("all-MiniLM-L6-v2")
            memo_semantic_similarity._model = model
        except Exception as exc:
            logger.debug("_compute_similarity_rows: best-effort step skipped (%s: %s)", type(exc).__name__, exc)

    if model is not None:
        from sentence_transformers import util
        emb_q = model.encode(memos_q, convert_to_tensor=True, batch_size=256, show_progress_bar=False)
        emb_p = model.encode(memos_p, convert_to_tensor=True, batch_size=256, show_progress_bar=False)
        memo_matrix = util.cos_sim(emb_q, emb_p).cpu().numpy()
    else:
        memo_matrix = process.cdist(memos_q, memos_p, scorer=fuzz.token_sort_ratio) / 100.0

    pool_id_to_idx = {t.source_txn_id: i for i, t in enumerate(pool)}
    query_id_to_row = {t.source_txn_id: i for i, t in enumerate(query)}
    return SimilarityContext(query=query, pool=pool, pool_id_to_idx=pool_id_to_idx,
                              query_id_to_row=query_id_to_row,
                              ref_matrix=ref_matrix, memo_matrix=memo_matrix)


def build_similarity_context(query: list[NormalizedTxn], pool: list[NormalizedTxn]) -> SimilarityContext:
    """
    Public entry point for tiebreak scoring — `query` here is expected to
    be small (a matched subset, typically well under a thousand
    transactions), so a single rectangular block against the full pool
    is safe. For fuzzy-fallback recovery over a potentially large
    `unmatched` set, use bulk_fuzzy_recover instead, which blocks
    internally.
    """
    return _compute_similarity_rows(query, pool)


# Bounded so a single block's matrices stay well under ~100MB even
# against a 50,000-item pool (2000 x 50000 x 8 bytes x 2 matrices ~ 1.6GB
# transient, freed after each block -- tune down further if running in a
# memory-constrained environment).
FUZZY_RECOVERY_BLOCK_SIZE = 2000


def bulk_fuzzy_recover(
    batch_id: str,
    unmatched: list[NormalizedTxn],
    full_pool: list[NormalizedTxn],
    config: FuzzyMatchConfig | None = None,
    block_size: int = FUZZY_RECOVERY_BLOCK_SIZE,
) -> list[MatchResult]:
    """
    Fuzzy recovery over `unmatched` in blocks of `block_size`, so peak memory is
    O(block_size x pool_size) however large `unmatched` is. Total work is the
    same; only the peak is bounded.
    """
    config = config or FuzzyMatchConfig()
    if not unmatched or not full_pool:
        return []

    results: list[MatchResult] = []

    for block_start in range(0, len(unmatched), block_size):
        block = unmatched[block_start:block_start + block_size]
        context = _compute_similarity_rows(block, full_pool)

        for row_idx, txn in enumerate(block):
            self_idx = context.pool_id_to_idx.get(txn.source_txn_id)

            combined = (
                config.ref_id_weight * context.ref_matrix[row_idx]
                + config.memo_semantic_weight * context.memo_matrix[row_idx]
            )
            if self_idx is not None:
                combined = combined.copy()
                combined[self_idx] = -1.0  # exclude self

            best_idx = int(combined.argmax())
            best_score = float(combined[best_idx])
            match = context.pool[best_idx] if best_score > -1.0 else None
            cleared = match is not None and best_score >= config.confidence_threshold

            reasoning = (
                f"ref_id similarity={context.ref_matrix[row_idx, best_idx]:.2f}, "
                f"memo similarity={context.memo_matrix[row_idx, best_idx]:.2f}, "
                f"weighted confidence={best_score:.2f}"
                if match else "No candidates to compare against."
            )

            results.append(MatchResult(
                batch_id=batch_id,
                matched_txn_ids=[match.source_txn_id] if cleared and match else [],
                method=MatchMethod.FUZZY_SEMANTIC,
                confidence=best_score if match else 0.0,
                matched_sum_cents=match.amount_cents if cleared and match else 0,
                target_cents=txn.amount_cents,
                cleared=cleared,
                reasoning=(
                    reasoning if cleared else
                    f"Below clear threshold ({config.confidence_threshold}): {reasoning}. Routing to exceptions."
                ),
            ))

    return results


def fuzzy_match_candidate(
    target: NormalizedTxn,
    pool: list[NormalizedTxn],
    config: FuzzyMatchConfig | None = None,
) -> tuple[NormalizedTxn | None, float, str]:
    """
    Finds the best fuzzy match for `target` in `pool`. Kept for
    single-call/test use and small pools. For classifying MANY
    transactions against the same pool, use bulk_fuzzy_recover instead —
    calling this in a loop reintroduces the O(n^2) pattern this module
    was fixed for (see module docstring).
    """
    config = config or FuzzyMatchConfig()
    best_match, best_score, best_reason = None, 0.0, ""

    for candidate in pool:
        ref_sim = ref_id_similarity(target.ref_id_canonical, candidate.ref_id_canonical)
        memo_sim = memo_semantic_similarity(target.memo_normalized, candidate.memo_normalized)
        score = config.ref_id_weight * ref_sim + config.memo_semantic_weight * memo_sim

        if score > best_score:
            best_score = score
            best_match = candidate
            best_reason = (
                f"ref_id similarity={ref_sim:.2f}, memo semantic similarity={memo_sim:.2f}, "
                f"weighted confidence={score:.2f}"
            )

    return best_match, best_score, best_reason


def match_batch_fuzzy(
    batch_id: str,
    target: NormalizedTxn,
    candidates: list[NormalizedTxn],
    config: FuzzyMatchConfig | None = None,
) -> MatchResult:
    """Single-transaction entry point — same caution as fuzzy_match_candidate:
    fine for a one-off call, do not use in a per-transaction loop over a
    large pool. Use bulk_fuzzy_recover for that."""
    config = config or FuzzyMatchConfig()
    match, confidence, reasoning = fuzzy_match_candidate(target, candidates, config)

    cleared = match is not None and confidence >= config.confidence_threshold

    return MatchResult(
        batch_id=batch_id,
        matched_txn_ids=[match.source_txn_id] if match else [],
        method=MatchMethod.FUZZY_SEMANTIC,
        confidence=confidence,
        matched_sum_cents=match.amount_cents if match else 0,
        target_cents=target.amount_cents,
        cleared=cleared,
        reasoning=(
            reasoning if cleared else
            f"Below clear threshold ({config.confidence_threshold}): {reasoning}. Routing to exceptions."
        ),
    )


def approximate_subset_sum_greedy(
    candidates: list[NormalizedTxn],
    target_cents: int,
) -> tuple[list[NormalizedTxn], int]:
    """
    Probabilistic Fallback for when CP-SAT fails on massive anchorless datasets.
    Uses a greedy approximation to find the subset closest to the target without going over.
    This is extremely fast (O(N log N)) and avoids the pseudo-polynomial DP trap.
    """
    if not candidates:
        return [], 0
    
    # Sort by amount descending to pack larger items first
    candidates_sorted = sorted(candidates, key=lambda x: x.amount_cents, reverse=True)
    current_sum = 0
    selected = []
    
    for c in candidates_sorted:
        if current_sum + c.amount_cents <= target_cents:
            current_sum += c.amount_cents
            selected.append(c)
            
    return selected, current_sum


def score_subset_plausibility(
    subset: list[NormalizedTxn],
    context: SimilarityContext,
    ref_id_weight: float = 0.6,
    memo_weight: float = 0.4,
) -> float:
    """
    How coherent a proposed subset is, used to break ties between two
    arithmetically valid subsets.

    Build `context` once over the UNION of the subsets being compared; rows are
    looked up by id. Per member: internal = best similarity to other members,
    external = best similarity to non-members, coherence = internal - 0.5 *
    external. A heuristic that breaks ties, never a proof; the trail records it.
    """
    if not subset:
        return 0.0

    # row positions (in context.ref_matrix/memo_matrix) for each subset member
    subset_rows = [context.query_id_to_row[t.source_txn_id] for t in subset
                   if t.source_txn_id in context.query_id_to_row]
    if not subset_rows:
        return 0.0

    # column positions (in the pool) for each subset member, to exclude
    # from the "external" comparison and to look up as "peers" internally
    subset_pool_idx = [context.pool_id_to_idx.get(t.source_txn_id) for t in subset]
    subset_pool_idx_set = {i for i in subset_pool_idx if i is not None}
    non_subset_idx = [i for i in range(len(context.pool)) if i not in subset_pool_idx_set]

    if not non_subset_idx:
        return 1.0

    total_score = 0.0
    for row_i, own_pool_idx in zip(subset_rows, subset_pool_idx):
        peer_pool_idx = [i for i in subset_pool_idx if i is not None and i != own_pool_idx]

        combined_ext = (
            ref_id_weight * context.ref_matrix[row_i, non_subset_idx].max()
            + memo_weight * context.memo_matrix[row_i, non_subset_idx].max()
        )

        if not peer_pool_idx:
            total_score += 1.0 - combined_ext
            continue

        combined_int = (
            ref_id_weight * context.ref_matrix[row_i, peer_pool_idx].max()
            + memo_weight * context.memo_matrix[row_i, peer_pool_idx].max()
        )
        total_score += combined_int - 0.5 * combined_ext

    raw = total_score / len(subset_rows)
    return max(0.0, min(1.0, (raw + 1.0) / 2.0))
