import os
import json
import logging
from typing import List, Dict

from kafka import KafkaConsumer
import redis
from redis.exceptions import ConnectionError

import orchestrator
from schema import NormalizedTxn, SourceType, ComplianceStatus, TzConfidence
from dateutil.parser import parse

logger = logging.getLogger(__name__)

KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "localhost:9092")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
NORMALIZED_TOPIC = "normalized-transactions"

def get_redis_client() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)

def create_kafka_consumer() -> KafkaConsumer:
    return KafkaConsumer(
        NORMALIZED_TOPIC,
        bootstrap_servers=KAFKA_BROKER,
        auto_offset_reset='earliest',
        enable_auto_commit=True,
        group_id='reconciliation-workers',
        value_deserializer=lambda x: json.loads(x.decode('utf-8')),
        consumer_timeout_ms=5000 # Stop blocking after 5 seconds of silence
    )

def _deserialize_txn(payload: dict) -> NormalizedTxn:
    # Reconstruct the dataclass from JSON
    return NormalizedTxn(
        source=SourceType(payload["source"]),
        source_txn_id=payload["source_txn_id"],
        ref_id_canonical=payload["ref_id_canonical"],
        amount_cents=payload["amount_cents"],
        currency=payload["currency"],
        timestamp_utc=parse(payload["timestamp_utc"]),
        tz_confidence=TzConfidence(payload["tz_confidence"]) if payload.get("tz_confidence") else None,
        currency_stated=payload.get("currency_stated", True),
        memo_raw=payload.get("memo_raw", ""),
        memo_normalized=payload.get("memo_normalized", ""),
        payer_id=payload.get("payer_id", ""),
        payee_id=payload.get("payee_id", ""),
        is_cash=payload.get("is_cash", False),
        is_wire_transfer=payload.get("is_wire_transfer", False),
        compliance_status=ComplianceStatus(payload.get("compliance_status", "pass")),
        extra=payload.get("extra", {})
    )

def consume_and_reconcile():
    """
    Kafka Reconciliation Worker (Stateless Kubernetes Pod).
    Pulls normalized transactions from Kafka, acquires a Redis lock, and executes
    the CP-SAT deterministic matching pipeline.
    """
    try:
        consumer = create_kafka_consumer()
        redis_client = get_redis_client()
        redis_client.ping() # Test connection
    except Exception as e:
        logger.warning(f"Message broker or Redis not reachable, worker running in dry mode: {e}")
        return

    logger.info("Reconciliation worker started, listening on %s", NORMALIZED_TOPIC)
    
    # In a real streaming architecture, we would window the stream (e.g. tumbling window)
    # Here we buffer until the consumer times out (batch completion heuristic).
    buffer: List[NormalizedTxn] = []
    
    for message in consumer:
        buffer.append(_deserialize_txn(message.value))
        
    if not buffer:
        logger.info("No messages in topic to reconcile.")
        return
        
    logger.info(f"Buffered {len(buffer)} transactions. Preparing to reconcile...")
    
    # Separate the pool into targets (Bank Settlements) and candidates (Gateway/ERP legs)
    targets = [t for t in buffer if t.source == SourceType.BANK]
    candidates = [t for t in buffer if t.source != SourceType.BANK]
    
    if not targets:
        logger.warning("No settlement targets found in buffer. Cannot reconcile.")
        return
        
    # Attempt to process each settlement target
    for target_txn in targets:
        batch_id = target_txn.source_txn_id
        lock_key = f"recon_lock:{batch_id}"
        
        # Redis Distributed Lock (ensures exactly-once processing across N workers)
        acquired = redis_client.set(lock_key, "locked", nx=True, ex=300)
        
        if not acquired:
            logger.info(f"Batch {batch_id} is already being reconciled by another worker. Skipping.")
            continue
            
        try:
            logger.info(f"Worker acquired lock for batch {batch_id}. Starting CP-SAT...")
            
            # Map the NormalizedTxn target back into a SettlementBatch
            from schema import SettlementBatch
            batch = SettlementBatch(
                batch_id=batch_id,
                net_amount_cents=target_txn.amount_cents,
                currency=target_txn.currency,
                settled_at_utc=target_txn.timestamp_utc,
                source=target_txn.source,
                memo=target_txn.memo_raw,
                ref_id=target_txn.ref_id_canonical
            )
            
            # Run Orchestrator Pipeline
            report = orchestrator.reconcile_batch(batch, candidates)
            
            # We would typically publish this report back to a `reconciliation-reports` topic,
            # or save it directly to the PostgreSQL database.
            logger.info(f"Batch {batch_id} reconciled: Cleared={report.match_result.cleared}, Method={report.match_result.method.value}")
            
        finally:
            redis_client.delete(lock_key)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    consume_and_reconcile()
