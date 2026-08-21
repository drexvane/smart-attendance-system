"""Flask extension singletons.

Declared unbound so that :func:`app.create_app` can attach them to a fresh app per
test.  Nothing here is optional -- the previous code wrapped Flask-WTF and
Flask-Limiter in ``try/except ImportError`` and set a module flag, which meant CSRF
protection and rate limiting could silently vanish in production if a dependency
failed to install.  A missing dependency should break the build, not the security
model.
"""

from __future__ import annotations

import sqlite3

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import MetaData, event
from sqlalchemy.engine import Engine

# Explicit constraint naming.  SQLite cannot ALTER a constraint in place, so Alembic
# has to drop and recreate it inside a batch operation -- which only works if the
# constraint has a predictable name.  Without this, adding the unique
# (student_id, date) constraint to the existing attendance table is not migratable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

db = SQLAlchemy(metadata=MetaData(naming_convention=NAMING_CONVENTION))
migrate = Migrate()
csrf = CSRFProtect()
login_manager = LoginManager()
limiter = Limiter(key_func=get_remote_address)


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, connection_record):  # noqa: ARG001
    """Turn on foreign key enforcement for SQLite connections.

    SQLite parses ``ON DELETE CASCADE`` but ignores it unless this pragma is set, per
    connection.  Without it the cascade in the schema is decorative: deleting a student
    would leave their attendance and leave rows behind pointing at a row that no longer
    exists -- the same orphaned-data problem the old hand-written deletes had.
    """
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


login_manager.login_view = "auth.login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"
login_manager.session_protection = "strong"
