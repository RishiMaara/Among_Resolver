import pytest
from schema import ExceptionRecord, ExceptionReason, ExceptionStatus
from exception_workflow import propose_resolution, approve_resolution, reject_resolution, WorkflowError
from rbac import set_current_user, UserRole

@pytest.fixture
def sample_exception():
    return ExceptionRecord(
        batch_id="batch_123",
        candidate_txn_ids=["txn_1"],
        reason=ExceptionReason.TIMING_LAG,
        diagnosis_note="Missing leg",
    )

def test_maker_can_propose_resolution(sample_exception):
    set_current_user("alice", UserRole.MAKER)
    propose_resolution(sample_exception, "Found the missing invoice")
    
    assert sample_exception.status == ExceptionStatus.PENDING_APPROVAL
    assert sample_exception.maker_id == "alice"
    assert sample_exception.resolution_note == "Found the missing invoice"


def test_checker_can_approve_different_makers_resolution(sample_exception):
    set_current_user("alice", UserRole.MAKER)
    propose_resolution(sample_exception, "Found it")
    
    set_current_user("bob", UserRole.CHECKER)
    approve_resolution(sample_exception)
    
    assert sample_exception.status == ExceptionStatus.RESOLVED
    assert sample_exception.checker_id == "bob"


def test_checker_cannot_approve_own_resolution(sample_exception):
    # A user happens to have both Maker and Checker roles conceptually, 
    # but here they act as a Maker.
    set_current_user("alice_dual_role", UserRole.MAKER)
    propose_resolution(sample_exception, "I made this")
    
    # Alice tries to approve her own work by switching hats to Checker
    set_current_user("alice_dual_role", UserRole.CHECKER)
    with pytest.raises(WorkflowError, match="Maker-Checker violation"):
        approve_resolution(sample_exception)


def test_checker_can_reject_resolution(sample_exception):
    set_current_user("alice", UserRole.MAKER)
    propose_resolution(sample_exception, "I think this is okay")
    
    set_current_user("bob", UserRole.CHECKER)
    reject_resolution(sample_exception, "Missing documentation")
    
    assert sample_exception.status == ExceptionStatus.OPEN
    assert sample_exception.maker_id is None
    assert sample_exception.resolution_note is None


def test_system_role_cannot_propose_or_approve(sample_exception):
    set_current_user("system_bot", UserRole.SYSTEM)
    with pytest.raises(Exception):
        propose_resolution(sample_exception, "Bot action")
        
    set_current_user("alice", UserRole.MAKER)
    propose_resolution(sample_exception, "Human action")
    
    set_current_user("system_bot", UserRole.SYSTEM)
    with pytest.raises(Exception):
        approve_resolution(sample_exception)
