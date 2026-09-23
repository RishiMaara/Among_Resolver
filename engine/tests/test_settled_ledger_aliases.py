"""
The paid-out ledger answers for a payment however it was recorded.

A clear records the collision-safe key ("gateway:1001"); a FIFO acceptance,
and rows written before keys existed, hold the bare id. Open items and the
investigator's paid-out check ask by bare id. Without aliases, a payment paid
out once looked unpaid to every caller asking the other way (FAILURE_LOG 43).
"""
from uuid import uuid4

import settled_ledger


def _ids():
    return f"ALIAS-{uuid4().hex[:10]}", f"T{uuid4().hex[:10]}"


def test_recorded_by_key_found_by_bare_id():
    batch, txn = _ids()
    settled_ledger.record_settled(batch, [f"gateway:{txn}"])
    assert settled_ledger.owners([txn]) == {txn: batch}
    assert settled_ledger.check_claims("ANOTHER-BATCH", [txn])["count"] == 1


def test_recorded_by_bare_id_found_by_key():
    batch, txn = _ids()
    settled_ledger.record_settled(batch, [txn])
    assert settled_ledger.owners([f"erp:{txn}"]) == {f"erp:{txn}": batch}
    # A batch is never in conflict with its own earlier claim.
    assert settled_ledger.check_claims(batch, [f"gateway:{txn}"])["count"] == 0
