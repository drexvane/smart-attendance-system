"""Database models.

Split by aggregate so that importing one does not drag in the others.  Import from
this package (``from app.models import Student``) rather than from the submodules.
"""

from __future__ import annotations

from app.models.attendance import AttendanceRecord, AttendanceSession
from app.models.leave import LeaveRequest
from app.models.student import Student
from app.models.user import Role, User

__all__ = [
    "AttendanceRecord",
    "AttendanceSession",
    "LeaveRequest",
    "Role",
    "Student",
    "User",
]
