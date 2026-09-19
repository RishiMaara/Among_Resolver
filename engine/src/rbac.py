from enum import Enum
from dataclasses import dataclass
from typing import Optional

class UserRole(str, Enum):
    SYSTEM = "system"       # Automated jobs (e.g. AI agents, orchestrator)
    MAKER = "maker"         # Analyst (can propose resolutions)
    CHECKER = "checker"     # Controller (can approve resolutions)
    ADMIN = "admin"         # Root access

@dataclass(frozen=True)
class UserContext:
    user_id: str
    role: UserRole

class AccessDeniedError(Exception):
    """Raised when a user attempts an action outside their role."""
    pass

def require_role(context: UserContext, allowed_roles: set[UserRole]) -> None:
    if context.role not in allowed_roles and context.role != UserRole.ADMIN:
        raise AccessDeniedError(
            f"User {context.user_id} with role {context.role.value} is not authorized. "
            f"Requires one of: {[r.value for r in allowed_roles]}"
        )

# Global context mock for testing/demonstration
_current_user: Optional[UserContext] = None

def set_current_user(user_id: str, role: UserRole) -> None:
    global _current_user
    _current_user = UserContext(user_id, role)

def get_current_user() -> UserContext:
    if _current_user is None:
        return UserContext(user_id="system_auto", role=UserRole.SYSTEM)
    return _current_user
