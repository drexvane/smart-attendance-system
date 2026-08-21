"""Authentication, indexes, the daily uniqueness constraint, and face enrolment columns.

Revision ID: 0003_auth_and_constraints
Revises: 0002_dedupe_attendance
Create Date: 2026-08-21

Brings the pre-refactor schema up to the 2.0 models:

* ``users`` -- there was no authentication at all before this.
* ``uq_attendance_student_date`` -- the constraint that makes marking idempotent.
* Indexes on the columns every filter and report actually used (``date``, ``status``,
  ``student_id``, ``department``, ``year``, ``is_active``); the old table had none, so
  the attendance page scanned it in full.
* ``attendance_records.marked_by`` -- read by the export code via
  ``getattr(record, 'marked_by', 'System')`` and set by one route, but never defined, so
  exports always said "System" and that route raised a TypeError.
* Face enrolment bookkeeping on ``students``, replacing ``face_encoding``.

``face_encoding`` is dropped rather than cleared.  It held a 256-bin grayscale intensity
histogram, which carries no spatial information and matched near-anything; keeping the
column would only invite someone to trust it again.  The face samples the new LBPH
recognizer needs live under ``face_data/samples/<student_pk>/`` and are rebuilt with
``flask rebuild-faces``.

All table alterations run inside ``batch_alter_table`` because SQLite cannot add a
constraint or drop a column in place -- it has to rebuild the table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_auth_and_constraints"
down_revision = "0002_dedupe_attendance"
branch_labels = None
depends_on = None

# Databases created by the old ``db.create_all()`` have *unnamed* foreign keys
# (``FOREIGN KEY(student_id) REFERENCES students (id)`` with no CONSTRAINT clause), so
# ``drop_constraint`` cannot find them by name.  Handing the convention to
# ``batch_alter_table`` lets Alembic reconstruct the implied name and drop them.
FK_NAMING = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}

# The students table also carries unnamed UNIQUE constraints on student_id and email in
# old databases.  Including the "uq" rule lets the batch rebuild re-emit them with the
# names the models expect, so a migrated database ends up byte-identical to a fresh one.
UQ_NAMING = {**FK_NAMING, "uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    # --- operator accounts ----------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="teacher"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    # --- students -------------------------------------------------------------
    # Backfill before tightening nullability; rows written by the old code could have
    # NULL timestamps because the defaults were applied only at the Python layer.
    op.execute("UPDATE students SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
    op.execute("UPDATE students SET updated_at = created_at WHERE updated_at IS NULL")
    op.execute("UPDATE students SET is_active = 1 WHERE is_active IS NULL")

    with op.batch_alter_table("students", naming_convention=UQ_NAMING) as batch:
        batch.add_column(sa.Column("face_model_label", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "face_samples_count", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.add_column(sa.Column("face_enrolled_at", sa.DateTime(timezone=True), nullable=True))
        batch.alter_column("image_path", type_=sa.String(length=255), existing_nullable=True)
        batch.alter_column(
            "is_active", existing_type=sa.Boolean(), nullable=False, server_default=sa.true()
        )
        batch.alter_column("created_at", existing_type=sa.DateTime(), nullable=False)
        batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)
        batch.drop_column("face_encoding")
        batch.create_unique_constraint("uq_students_face_model_label", ["face_model_label"])
        batch.create_index("ix_students_name", ["name"])
        batch.create_index("ix_students_department", ["department"])
        batch.create_index("ix_students_year", ["year"])
        batch.create_index("ix_students_is_active", ["is_active"])

    # --- attendance -----------------------------------------------------------
    # Backfill before making the column NOT NULL, otherwise existing rows fail.
    op.add_column(
        "attendance_records",
        sa.Column("marked_by", sa.String(length=64), nullable=True),
    )
    op.add_column("attendance_records", sa.Column("notes", sa.String(length=255), nullable=True))
    op.add_column(
        "attendance_records", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE attendance_records SET marked_by = 'System' WHERE marked_by IS NULL")
    op.execute("UPDATE attendance_records SET status = 'Present' WHERE status IS NULL")
    op.execute(
        "UPDATE attendance_records SET created_at = time_in WHERE created_at IS NULL"
    )
    op.execute(
        "UPDATE attendance_records SET updated_at = COALESCE(created_at, time_in) "
        "WHERE updated_at IS NULL"
    )

    with op.batch_alter_table("attendance_records", naming_convention=FK_NAMING) as batch:
        batch.alter_column(
            "marked_by", existing_type=sa.String(length=64), nullable=False, server_default="System"
        )
        batch.alter_column("status", existing_type=sa.String(length=20), nullable=False)
        batch.alter_column("created_at", existing_type=sa.DateTime(), nullable=False)
        batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)
        # This is the constraint that turns "SELECT then INSERT" into a single
        # idempotent write.  0002 removed the duplicates that would block it.
        batch.create_unique_constraint("uq_attendance_student_date", ["student_id", "date"])
        # ON DELETE CASCADE so a permanent student deletion cannot leave orphaned rows.
        # The old code deleted attendance by hand and forgot leave requests entirely,
        # leaving dangling foreign keys behind.
        batch.drop_constraint("fk_attendance_records_student_id_students", type_="foreignkey")
        batch.create_foreign_key(
            "fk_attendance_records_student_id_students",
            "students",
            ["student_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_index("ix_attendance_records_date", ["date"])
        batch.create_index("ix_attendance_records_status", ["status"])
        batch.create_index("ix_attendance_records_student_id", ["student_id"])
        batch.create_index("ix_attendance_date_status", ["date", "status"])

    # --- leave ----------------------------------------------------------------
    op.execute("UPDATE leave_requests SET status = 'Pending' WHERE status IS NULL")
    op.execute("UPDATE leave_requests SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
    op.execute("UPDATE leave_requests SET updated_at = created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("leave_requests", naming_convention=FK_NAMING) as batch:
        batch.alter_column("status", existing_type=sa.String(length=20), nullable=False)
        batch.alter_column("created_at", existing_type=sa.DateTime(), nullable=False)
        batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=False)
        batch.drop_constraint("fk_leave_requests_student_id_students", type_="foreignkey")
        batch.create_foreign_key(
            "fk_leave_requests_student_id_students",
            "students",
            ["student_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_check_constraint("dates_ordered", "end_date >= start_date")
        batch.create_index("ix_leave_requests_student_id", ["student_id"])
        batch.create_index("ix_leave_requests_status", ["status"])
        batch.create_index("ix_leave_student_status", ["student_id", "status"])
        batch.create_index("ix_leave_window", ["start_date", "end_date"])

    # --- sessions -------------------------------------------------------------
    op.execute("UPDATE attendance_sessions SET is_active = 1 WHERE is_active IS NULL")
    op.execute(
        "UPDATE attendance_sessions SET created_at = start_time WHERE created_at IS NULL"
    )
    with op.batch_alter_table("attendance_sessions") as batch:
        batch.alter_column(
            "is_active", existing_type=sa.Boolean(), nullable=False, server_default=sa.true()
        )
        batch.alter_column("created_at", existing_type=sa.DateTime(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("attendance_sessions") as batch:
        batch.alter_column("created_at", existing_type=sa.DateTime(), nullable=True)
        batch.alter_column("is_active", existing_type=sa.Boolean(), nullable=True)

    with op.batch_alter_table("leave_requests", naming_convention=FK_NAMING) as batch:
        batch.drop_index("ix_leave_window")
        batch.drop_index("ix_leave_student_status")
        batch.drop_index("ix_leave_requests_status")
        batch.drop_index("ix_leave_requests_student_id")
        batch.drop_constraint("ck_leave_requests_dates_ordered", type_="check")
        batch.drop_constraint("fk_leave_requests_student_id_students", type_="foreignkey")
        batch.create_foreign_key(
            "fk_leave_requests_student_id_students", "students", ["student_id"], ["id"]
        )
        batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=True)
        batch.alter_column("created_at", existing_type=sa.DateTime(), nullable=True)
        batch.alter_column("status", existing_type=sa.String(length=20), nullable=True)

    with op.batch_alter_table("attendance_records", naming_convention=FK_NAMING) as batch:
        batch.drop_index("ix_attendance_date_status")
        batch.drop_index("ix_attendance_records_student_id")
        batch.drop_index("ix_attendance_records_status")
        batch.drop_index("ix_attendance_records_date")
        batch.drop_constraint("fk_attendance_records_student_id_students", type_="foreignkey")
        batch.create_foreign_key(
            "fk_attendance_records_student_id_students", "students", ["student_id"], ["id"]
        )
        batch.drop_constraint("uq_attendance_student_date", type_="unique")
        batch.alter_column("status", existing_type=sa.String(length=20), nullable=True)
        batch.drop_column("updated_at")
        batch.drop_column("notes")
        batch.drop_column("marked_by")

    with op.batch_alter_table("students", naming_convention=UQ_NAMING) as batch:
        batch.drop_index("ix_students_is_active")
        batch.drop_index("ix_students_year")
        batch.drop_index("ix_students_department")
        batch.drop_index("ix_students_name")
        batch.drop_constraint("uq_students_face_model_label", type_="unique")
        batch.alter_column("updated_at", existing_type=sa.DateTime(), nullable=True)
        batch.alter_column("created_at", existing_type=sa.DateTime(), nullable=True)
        # Recreated empty: the original histograms were not recoverable and were not
        # worth recovering.
        batch.add_column(sa.Column("face_encoding", sa.Text(), nullable=True))
        batch.drop_column("face_enrolled_at")
        batch.drop_column("face_samples_count")
        batch.drop_column("face_model_label")

    op.drop_table("users")
