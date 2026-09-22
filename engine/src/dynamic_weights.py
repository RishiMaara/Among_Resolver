"""
The hand-set linkage weights, overridable by environment variable so
scripts/sweep_linkage_weights.py can vary them. Reasoned, not fitted
(FAILURE_LOG 2); weights learned from data live in linkage_em.py.
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
