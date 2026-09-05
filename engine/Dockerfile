# The engine, deployable.
#
# "Runs on my laptop with two terminals" is not an operability story, and a
# judge cannot click a laptop. This is the smallest honest container: pinned
# base, no build toolchain in the final image, non-root, and a healthcheck
# that asks the readiness endpoint rather than just whether the port is open.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a source change does not reinstall ortools.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/

# The audit trail must outlive the container. Mount a volume here or the
# durability the engine reports is a container-lifetime durability, which is
# worse than none because it looks like the real thing.
RUN mkdir -p /data
ENV AUDIT_DB_PATH=/data/audit.sqlite3

# Non-root: a reconciliation engine has no reason to be able to write outside
# its own data directory.
RUN useradd --create-home --uid 10001 engine \
 && chown -R engine:engine /app /data
USER engine

EXPOSE 8001

# Readiness, not liveness. /health reports whether the audit store is durable
# and whether auth is on, so an orchestrator can refuse to route to a node
# that came up misconfigured.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "main:app", "--app-dir", "src", \
     "--host", "0.0.0.0", "--port", "8001"]
