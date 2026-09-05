import os
import sqlite3
import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Fallback defaults identical to current hardcoded values
DEFAULT_WEIGHTS = {
    "W_SETTLEMENT_ID": 0.55,
    "W_SHARED_REF_TOKEN": 0.25,
    "W_REF_PREFIX_CLUSTER": 0.15,
    "W_CROSS_SOURCE_AMOUNT": 0.10,
    "W_OUT_OF_WINDOW_PENALTY": 0.30,
}

def get_dynamic_weights(db_path: str = "audit.sqlite3") -> Dict[str, float]:
    """
    Dynamically computes linkage weights based on historical success rates
    if ENABLE_DYNAMIC_WEIGHTS is true. Otherwise returns defaults.
    """
    if os.environ.get("ENABLE_DYNAMIC_WEIGHTS", "0").strip() != "1":
        return {
            "W_SETTLEMENT_ID": float(os.environ.get("W_SETTLEMENT_ID", DEFAULT_WEIGHTS["W_SETTLEMENT_ID"])),
            "W_SHARED_REF_TOKEN": float(os.environ.get("W_SHARED_REF_TOKEN", DEFAULT_WEIGHTS["W_SHARED_REF_TOKEN"])),
            "W_REF_PREFIX_CLUSTER": float(os.environ.get("W_REF_PREFIX_CLUSTER", DEFAULT_WEIGHTS["W_REF_PREFIX_CLUSTER"])),
            "W_CROSS_SOURCE_AMOUNT": float(os.environ.get("W_CROSS_SOURCE_AMOUNT", DEFAULT_WEIGHTS["W_CROSS_SOURCE_AMOUNT"])),
            "W_OUT_OF_WINDOW_PENALTY": float(os.environ.get("W_OUT_OF_WINDOW_PENALTY", DEFAULT_WEIGHTS["W_OUT_OF_WINDOW_PENALTY"])),
        }

    weights = DEFAULT_WEIGHTS.copy()
    if not os.path.exists(db_path):
        logger.info(f"Dynamic Weights: {db_path} not found. Using defaults.")
        return weights

    try:
        # We look at historical exact subset sum clear rates to adjust weights.
        # This is a simplified safe implementation.
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # We count how many times 'linkage' agent logged a successful method.
        # For simplicity, if we have over 100 historical records of anchor hits, we boost it.
        # (A real implementation would train a logistic regression over MatchResult telemetry)
        
        cursor.execute("SELECT detail FROM audit_log WHERE agent = 'linkage'")
        rows = cursor.fetchall()
        
        settlement_id_hits = 0
        cluster_hits = 0
        total_runs = 0
        
        for row in rows:
            detail = row[0]
            total_runs += 1
            if "settlement_id_anchor" in detail:
                settlement_id_hits += 1
            if "reference_cluster" in detail:
                cluster_hits += 1
                
        if total_runs > 50:
            settlement_ratio = settlement_id_hits / total_runs
            if settlement_ratio > 0.8:
                weights["W_SETTLEMENT_ID"] = min(0.9, weights["W_SETTLEMENT_ID"] + 0.1)
                
            cluster_ratio = cluster_hits / total_runs
            if cluster_ratio > 0.5:
                weights["W_SHARED_REF_TOKEN"] = min(0.5, weights["W_SHARED_REF_TOKEN"] + 0.05)
                
        logger.info("Dynamic Weights enabled. Calibrated from historical data.")
    except Exception as e:
        logger.warning(f"Dynamic Weights calculation failed: {e}. Falling back to defaults.")
        
    return weights
