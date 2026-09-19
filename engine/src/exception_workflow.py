from schema import ExceptionRecord, ExceptionStatus
from rbac import UserContext, UserRole, require_role, get_current_user
import audit

class WorkflowError(Exception):
    pass


def propose_resolution(exception: ExceptionRecord, resolution_note: str, force_clear: bool = False) -> None:
    """
    A MAKER proposes how to resolve an exception.
    """
    context = get_current_user()
    require_role(context, {UserRole.MAKER})
    
    if exception.status != ExceptionStatus.OPEN:
        raise WorkflowError(f"Cannot propose resolution for an exception in state: {exception.status.value}")
        
    exception.status = ExceptionStatus.PENDING_APPROVAL
    exception.maker_id = context.user_id
    exception.resolution_note = resolution_note
    
    audit.log_decision(
        batch_id=exception.batch_id,
        agent=f"human_maker_{context.user_id}",
        detail=f"Proposed resolution: {resolution_note}. Force clear: {force_clear}"
    )


def approve_resolution(exception: ExceptionRecord) -> None:
    """
    A CHECKER approves the MAKER's proposed resolution.
    """
    context = get_current_user()
    require_role(context, {UserRole.CHECKER})
    
    if exception.status != ExceptionStatus.PENDING_APPROVAL:
        raise WorkflowError(f"Cannot approve an exception in state: {exception.status.value}")
        
    # MAKER != CHECKER restriction (4-eyes principle)
    if exception.maker_id == context.user_id and context.role != UserRole.ADMIN:
        raise WorkflowError("Maker-Checker violation: A user cannot approve their own proposed resolution.")
        
    exception.status = ExceptionStatus.RESOLVED
    exception.checker_id = context.user_id
    
    audit.log_decision(
        batch_id=exception.batch_id,
        agent=f"human_checker_{context.user_id}",
        detail=f"Approved resolution proposed by {exception.maker_id}"
    )


def reject_resolution(exception: ExceptionRecord, rejection_reason: str) -> None:
    """
    A CHECKER rejects the MAKER's proposed resolution and sends it back to OPEN.
    """
    context = get_current_user()
    require_role(context, {UserRole.CHECKER})
    
    if exception.status != ExceptionStatus.PENDING_APPROVAL:
        raise WorkflowError(f"Cannot reject an exception in state: {exception.status.value}")
        
    exception.status = ExceptionStatus.OPEN
    old_maker = exception.maker_id
    exception.maker_id = None
    exception.resolution_note = None
    
    audit.log_decision(
        batch_id=exception.batch_id,
        agent=f"human_checker_{context.user_id}",
        detail=f"Rejected resolution proposed by {old_maker}. Reason: {rejection_reason}"
    )
