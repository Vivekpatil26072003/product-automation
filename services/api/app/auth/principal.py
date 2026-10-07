"""Server-side authorization context. Built from the database on every request, so a removed role or
department grant takes effect on the next request (FR01, FR23, TC02). Never taken from client input.
"""

import uuid
from dataclasses import dataclass

from app.audit.service import Actor
from app.domain.enums import Role


@dataclass(frozen=True)
class Principal:
    membership_id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    subject: str
    display_name: str | None
    email: str | None
    roles: frozenset[Role]
    department_ids: frozenset[uuid.UUID]
    timezone: str
    auth_method: str

    @property
    def actor(self) -> Actor:
        return Actor("user", self.membership_id)

    def has_any(self, *roles: Role) -> bool:
        return bool(self.roles.intersection(roles))

    def can_access_department(self, department_id: uuid.UUID) -> bool:
        return department_id in self.department_ids

    def can_access_all(self, department_ids: set[uuid.UUID]) -> bool:
        """A combined scope (e.g. a multi-department report) needs a grant for every department."""
        return department_ids <= self.department_ids
