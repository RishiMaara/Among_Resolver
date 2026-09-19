import os
import json
import logging
from typing import Iterator

from kafka import KafkaConsumer, KafkaProducer
from schema import NormalizedTxn
import file_agent
import ingestion

logger = logging.getLogger(__name__)

KAFKA_BROKER = os.environ.get("KAFKA_BROKER", "localhost:9092")
RAW_TOPIC = "raw-transactions"
NORMALIZED_TOPIC = "normalized-transactions"

def create_kafka_producer() -> KafkaProducer:
    """Creates a producer for writing normalized records."""
    return KafkaProducer(
        bootstrap_servers=KAFKA_BROKER,
        value_serializer=lambda v: json.dumps(v, default=str).encode('utf-8')
    )

def create_kafka_consumer() -> KafkaConsumer:
    """Listens to raw webhook and file-drop payloads."""
    return KafkaConsumer(
        RAW_TOPIC,
        bootstrap_servers=KAFKA_BROKER,
        auto_offset_reset='earliest',
        enable_auto_commit=True,
        group_id='ingestion-group',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )


def consume_and_normalize(limit: int = -1):
    """
    Kafka ingestion worker.
    Replaces synchronous webhooks/files with a distributed streaming topology.
    """
    try:
        consumer = create_kafka_consumer()
        producer = create_kafka_producer()
    except Exception as e:
        logger.warning(f"Kafka not reachable, ingestion worker running in dry mode: {e}")
        return

    logger.info("Ingestion worker started, listening on %s", RAW_TOPIC)
    
    count = 0
    for message in consumer:
        payload = message.value
        source_name = payload.get("source", "unknown")
        raw_rows = payload.get("rows", [])
        
        logger.info(f"Received raw batch from {source_name}: {len(raw_rows)} records")
        
        # 1. AI Standardization (Agent 0)
        # Using the existing file_agent mapping logic which usually works on files, 
        # but here applied directly to the JSON payload.
        mapping = file_agent._infer_mapping_for_type(source_name) 
        
        # 2. Normalization (Agent 1)
        normalized_txns = ingestion.normalize_batch(
            source_name=source_name,
            rows=raw_rows,
            mapping=mapping
        )
        
        # 3. Publish to next pipeline stage
        for txn in normalized_txns:
            # We would serialize the dataclass to dict here, simplified for demo
            txn_dict = txn.__dict__.copy()
            # Convert enums and datetimes for JSON
            txn_dict['source'] = txn.source.value
            txn_dict['timestamp_utc'] = txn.timestamp_utc.isoformat()
            if txn.tz_confidence:
                txn_dict['tz_confidence'] = txn.tz_confidence.value
            txn_dict['compliance_status'] = txn.compliance_status.value
            
            producer.send(NORMALIZED_TOPIC, value=txn_dict)
            
        producer.flush()
        logger.info(f"Published {len(normalized_txns)} normalized transactions to {NORMALIZED_TOPIC}")
        
        count += 1
        if limit > 0 and count >= limit:
            break

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    consume_and_normalize()
