from prometheus_client import Counter, Histogram, Gauge, start_http_server
import logging

logger = logging.getLogger(__name__)

# --- Prometheus Metrics ---

# 1. Matching KPIs
RECONCILIATION_ATTEMPTS = Counter(
    "amongresolver_recon_attempts_total",
    "Total number of reconciliation attempts",
    ["method", "outcome"]
)
AUTO_CLEAR_RATE = Gauge(
    "amongresolver_auto_clear_percentage",
    "Rolling 24h percentage of batches automatically cleared without human intervention"
)

# 2. Performance & Health
SOLVER_LATENCY = Histogram(
    "amongresolver_solver_duration_seconds",
    "Time spent in the CP-SAT subset-sum solver",
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 30.0]
)
SOLVER_TIMEOUTS = Counter(
    "amongresolver_solver_timeouts_total",
    "Number of times the solver hit its hard time limit"
)

# 3. Message Queue Health (Kafka/Event Sourcing)
CONSUMER_LAG = Gauge(
    "amongresolver_kafka_consumer_lag",
    "Number of messages behind the head of the topic",
    ["topic"]
)


def initialize_telemetry(port: int = 9090):
    """
    Initializes the observability stack. 
    Starts a Prometheus metrics server on the specified port.
    In a full deployment, this would also configure OpenTelemetry OTLP exporters
    for Jaeger/Zipkin tracing.
    """
    try:
        start_http_server(port)
        logger.info(f"Telemetry initialized. Prometheus metrics exposed on port {port}")
    except OSError as e:
        logger.warning(f"Could not start metrics server (port {port} may be in use): {e}")

# Usage examples to be called by the orchestrator:
def record_reconciliation_attempt(method: str, cleared: bool):
    outcome = "cleared" if cleared else "withheld"
    RECONCILIATION_ATTEMPTS.labels(method=method, outcome=outcome).inc()

def record_solver_time(duration_seconds: float):
    SOLVER_LATENCY.observe(duration_seconds)
    
def record_solver_timeout():
    SOLVER_TIMEOUTS.inc()
