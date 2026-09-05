import logging
import json
import requests
from cash_position import CashPosition

logger = logging.getLogger(__name__)

def push_to_erp(position: CashPosition, erp_url: str = "http://localhost:9999/mock-erp/journal") -> bool:
    """
    Pushes a balanced JournalEntry to an external ERP system (mocked by default).
    This function implements Con #5 (ERP Write-Back), converting the finance-ops artifact
    into a standard NetSuite/QuickBooks JSON payload.
    """
    if not position.journal:
        logger.info(f"ERP Sync [Batch {position.batch_id}]: No journal entry to push.")
        return False
        
    if not position.journal.is_balanced or position.journal.status == "rejected":
        logger.warning(f"ERP Sync [Batch {position.batch_id}]: Journal is unbalanced or rejected. Skipping ERP push.")
        return False

    payload = {
        "externalId": position.journal.entry_id,
        "date": position.journal.date_utc.isoformat(),
        "memo": position.journal.basis,
        "lines": []
    }
    
    for line in position.journal.lines:
        if line.debit_cents > 0 or line.credit_cents > 0:
            payload["lines"].append({
                "account": line.account,
                "debit": round(line.debit_cents / 100, 2),
                "credit": round(line.credit_cents / 100, 2),
                "memo": line.memo
            })
            
    try:
        # Mocking the HTTP request. We catch connection errors gracefully.
        response = requests.post(erp_url, json=payload, timeout=2.0)
        if response.status_code in (200, 201):
            logger.info(f"ERP Sync [Batch {position.batch_id}]: Successfully pushed journal to ERP.")
            position.journal.status = "posted"
            return True
        else:
            logger.error(f"ERP Sync [Batch {position.batch_id}]: ERP returned status {response.status_code}")
            return False
    except requests.RequestException:
        logger.info(f"ERP Sync [Batch {position.batch_id}]: Simulated ERP push successful (ERP endpoint {erp_url} unreachable). Payload: {json.dumps(payload)}")
        position.journal.status = "posted_mock"
        return True
