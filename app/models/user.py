"""Operator accounts.

The system previously had no authentication at all: any anonymous visitor could delete
students, rewrite attendance and export the whole roster.  Two roles are enough here --
a teacher runs attendance day to day, an admin can also destroy data and manage users.
"""

from __future__ import annotations

from enum import StrEnum

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.utils.time import utcnow


class Role(StrEnum):
    ADMIN = "admin"
    TEACHER = "teacher"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    # UNIQUE provides the lookup index; no separate index=True needed.
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=Role.TEACHER.value)
    is_active_flag = db.Column("is_active", db.Boolean, nullable=False, default=True)
    last_login_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    # --- password handling -------------------------------------------------------
    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    # --- Flask-Login contract ----------------------------------------------------
    # UserMixin.is_active is a property, so the column is stored under a different
    # attribute name and surfaced here.  A deactivated account cannot log in.
    @property
    def is_active(self) -> bool:  # type: ignore[override]
        return bool(self.is_active_flag)

    # --- roles -------------------------------------------------------------------
    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value

    def has_role(self, *roles: str) -> bool:
        return self.role in roles

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "is_active": self.is_active,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.role})>"
