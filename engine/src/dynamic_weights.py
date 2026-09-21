"""
The hand-set linkage weights, overridable by environment variable.

These are reasoned, not fitted (LINKAGE.md, FAILURE_LOG entry 2), and the
override exists so scripts/sweep_linkage_weights.py can vary one at a time
without editing source.

There used to be a second path here, behind ENABLE_DYNAMIC_WEIGHTS, described
elsewhere as "machine-learned weights". It counted how often the words
"settlement_id_anchor" and "reference_cluster" appeared in the audit log and
nudged two weights by a fixed step. That is not learning, it was off by
default, and nothing measured it. Weights that ARE learned from data live in
linkage_em.py, where they are fitted per pool and reported with every run.
"""

import os

DEFAULT_WEIGHTS = {
    "W_SETTLEMENT_ID": 0.55,
    "W_SHARED_REF_TOKEN": 0.25,
    "W_REF_PREFIX_CLUSTER": 0.15,
    "W_CROSS_SOURCE_AMOUNT": 0.10,
    "W_OUT_OF_WINDOW_PENALTY": 0.30,
}


def get_dynamic_weights(db_path: str = "") -> dict[str, float]:
    """The defaults, with any W_* environment override applied."""
    return {name: float(os.environ.get(name, default))
            for name, default in DEFAULT_WEIGHTS.items()}
