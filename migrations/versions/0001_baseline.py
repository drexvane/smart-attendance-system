"""Baseline: the schema as it existed before the 2.0 refactor.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-08-21

This reproduces the pre-refactor tables exactly so that:

* a fresh database can be built from scratch with ``flask db upgrade``, and
* an existing database (which has no ``alembic_version`` table, because the old code
  called ``db.create_all()`` and never used migrations) can be brought under Alembic
  control with ``flask db stamp 0001_baseline`` and then upgraded.

Nothing here should be edited -- later revisions carry the changes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    # Guarded so that stamping an existing database and then re-running upgrade from
    # scratch on a fresh one both work.
    if "students" not in existing:
        op.create_table(
            "students",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("student_id", sa.String(length=20), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("email", sa.String(length=120), nullable=False),
            sa.Column("phone", sa.String(length=15), nullable=True),
            sa.Column("department", sa.String(length=50), nullable=True),
            sa.Column("year", sa.String(length=10), nullable=True),
            sa.Column("section", sa.String(length=5), nullable=True),
            sa.Column("face_encoding", sa.Text(), nullable=True),
            sa.Column("image_path", sa.String(length=200), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id", name="pk_students"),
            sa.UniqueConstraint("student_id", name="uq_students_student_id"),
            sa.UniqueConstraint("email", name="uq_students_email"),
        )

    if "attendance_records" not in existing:
        op.create_table(
            "attendance_records",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("time_in", sa.DateTime(), nullable=False),
            sa.Column("time_out", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=True),
            sa.Column("confidence_score", sa.Float(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id", name="pk_attendance_records"),
            sa.ForeignKeyConstraint(
                ["student_id"],
                ["students.id"],
                name="fk_attendance_records_student_id_students",
            ),
        )

    if "leave_requests" not in existing:
        op.create_table(
            "leave_requests",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("student_id", sa.Integer(), nullable=False),
            sa.Column("leave_type", sa.String(length=50), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=True),
            sa.Column("reviewed_by", sa.String(length=100), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.Column("review_notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id", name="pk_leave_requests"),
            sa.ForeignKeyConstraint(
                ["student_id"], ["students.id"], name="fk_leave_requests_student_id_students"
            ),
        )

    if "attendance_sessions" not in existing:
        op.create_table(
            "attendance_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("session_name", sa.String(length=100), nullable=False),
            sa.Column("subject", sa.String(length=100), nullable=True),
            sa.Column("teacher_name", sa.String(length=100), nullable=True),
            sa.Column("department", sa.String(length=50), nullable=True),
            sa.Column("year", sa.String(length=10), nullable=True),
            sa.Column("section", sa.String(length=5), nullable=True),
            sa.Column("start_time", sa.DateTime(), nullable=False),
            sa.Column("end_time", sa.DateTime(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id", name="pk_attendance_sessions"),
        )


def downgrade() -> None:
    op.drop_table("attendance_sessions")
    op.drop_table("leave_requests")
    op.drop_table("attendance_records")
    op.drop_table("students")
