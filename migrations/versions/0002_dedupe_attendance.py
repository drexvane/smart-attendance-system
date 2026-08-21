"""De-duplicate attendance before the unique constraint can be added.

Revision ID: 0002_dedupe_attendance
Revises: 0001_baseline
Create Date: 2026-08-21

Every marking path in the old code did "SELECT then INSERT" with no constraint behind
it, so concurrent requests -- and the recognition loop, which polled and marked several
times a second -- could write more than one row per student per day.  Those duplicates
inflated every attendance percentage the analytics endpoints reported.

The next revision adds ``uq_attendance_student_date``, which cannot be created while
duplicates exist.  This revision collapses each ``(student_id, date)`` group down to its
earliest row and reports how many it removed, so an operator sees whether their data was
affected.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_dedupe_attendance"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    duplicates = bind.execute(
        sa.text(
            """
            SELECT student_id, date, COUNT(*) AS copies
            FROM attendance_records
            GROUP BY student_id, date
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()

    if not duplicates:
        print("0002: no duplicate attendance rows found")
        return

    total_extra = sum(row.copies - 1 for row in duplicates)
    print(
        f"0002: found {len(duplicates)} duplicated (student_id, date) group(s), "
        f"removing {total_extra} extra row(s); keeping the earliest of each"
    )

    # "Earliest" is the lowest id within the group, which for an autoincrement primary
    # key is the first row written.  Deliberately keeps the original mark rather than a
    # later correction, matching what the read-then-write code intended to do.
    result = bind.execute(
        sa.text(
            """
            DELETE FROM attendance_records
            WHERE id NOT IN (
                SELECT MIN(id) FROM attendance_records GROUP BY student_id, date
            )
            """
        )
    )
    print(f"0002: deleted {result.rowcount} row(s)")


def downgrade() -> None:
    # Deleted rows cannot be reconstructed. Nothing to do.
    pass
