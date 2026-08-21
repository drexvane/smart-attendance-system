"""Leave requests and their effect on attendance.

Dates here come from ``app_today()`` (the application clock, resolved through
``APP_TIMEZONE``) and not from ``app_today()``.  They are not interchangeable: on a
machine whose local zone runs ahead of ``APP_TIMEZONE`` they name different days for
part of every day, and ``on_leave_today()`` then finds nothing for a leave the test
believes it filed for today.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.extensions import db
from app.models import AttendanceRecord, LeaveRequest
from app.services import leave_service
from app.services.leave_service import LeaveError
from tests.conftest import app_today


def form(students, **overrides) -> dict:
    today = app_today()
    data = {
        "student_id": str(students[0].id),
        "leave_type": "Sick",
        "start_date": today.isoformat(),
        "end_date": (today + timedelta(days=2)).isoformat(),
        "reason": "Doctor has advised three days of rest.",
    }
    data.update(overrides)
    return data


class TestApplying:
    def test_creates_a_pending_request(self, admin_client, students):
        response = admin_client.post("/apply_leave", data=form(students), follow_redirects=True)
        assert response.status_code == 200

        request_row = db.session.execute(db.select(LeaveRequest)).scalar_one()
        assert request_row.status == "Pending"
        assert request_row.duration_days == 3

    def test_rejects_an_overlapping_request(self, admin_client, students):
        admin_client.post("/apply_leave", data=form(students), follow_redirects=True)
        admin_client.post(
            "/apply_leave",
            data=form(students, start_date=(app_today() + timedelta(days=1)).isoformat()),
            follow_redirects=True,
        )
        assert len(db.session.execute(db.select(LeaveRequest)).all()) == 1

    def test_rejects_a_short_reason(self, admin_client, students):
        admin_client.post("/apply_leave", data=form(students, reason="ill"), follow_redirects=True)
        assert db.session.execute(db.select(LeaveRequest)).first() is None

    def test_rejects_an_end_before_the_start(self, admin_client, students):
        admin_client.post(
            "/apply_leave",
            data=form(students, end_date=(app_today() - timedelta(days=5)).isoformat()),
            follow_redirects=True,
        )
        assert db.session.execute(db.select(LeaveRequest)).first() is None

    def test_rejects_an_unknown_leave_type(self, admin_client, students):
        admin_client.post(
            "/apply_leave", data=form(students, leave_type="Sabbatical"), follow_redirects=True
        )
        assert db.session.execute(db.select(LeaveRequest)).first() is None

    def test_rejects_an_inactive_student(self, app, students, db):
        students[0].is_active = False
        db.session.commit()

        with pytest.raises(LeaveError, match="active student"):
            leave_service.apply_for_leave(
                {
                    "student_id": students[0].id,
                    "leave_type": "Sick",
                    "start_date_parsed": app_today(),
                    "end_date_parsed": app_today(),
                    "reason": "Long enough reason here.",
                }
            )


class TestReviewing:
    def _pending(self, students) -> LeaveRequest:
        today = app_today()
        return leave_service.apply_for_leave(
            {
                "student_id": students[0].id,
                "leave_type": "Sick",
                "start_date_parsed": today,
                "end_date_parsed": today + timedelta(days=2),
                "reason": "Doctor has advised three days of rest.",
            }
        )

    def test_approval_writes_on_leave_attendance(self, app, students):  # noqa: ARG002
        request_row = self._pending(students)

        leave_service.review_leave(request_row.id, "Approved", reviewer="admin")

        records = db.session.execute(db.select(AttendanceRecord)).scalars().all()
        assert len(records) == 3
        assert {record.status for record in records} == {"On Leave"}

    def test_approval_overwrites_an_existing_mark(self, app, students, db):
        """A day already marked Present must become On Leave, not be skipped."""
        today = app_today()
        db.session.add(
            AttendanceRecord(student_id=students[0].id, date=today, status="Present", marked_by="t")
        )
        db.session.commit()
        request_row = self._pending(students)

        leave_service.review_leave(request_row.id, "Approved", reviewer="admin")

        row = db.session.execute(
            db.select(AttendanceRecord).filter_by(student_id=students[0].id, date=today)
        ).scalar_one()
        assert row.status == "On Leave"

    def test_approval_does_not_duplicate_rows(self, app, students):  # noqa: ARG002
        request_row = self._pending(students)
        leave_service.review_leave(request_row.id, "Approved", reviewer="admin")

        total = db.session.execute(
            db.select(db.func.count()).select_from(AttendanceRecord)
        ).scalar_one()
        assert total == 3

    def test_rejection_writes_no_attendance(self, app, students):  # noqa: ARG002
        request_row = self._pending(students)
        leave_service.review_leave(request_row.id, "Rejected", reviewer="admin")

        assert db.session.execute(db.select(AttendanceRecord)).first() is None

    def test_a_request_cannot_be_reviewed_twice(self, app, students):  # noqa: ARG002
        request_row = self._pending(students)
        leave_service.review_leave(request_row.id, "Approved", reviewer="admin")

        with pytest.raises(LeaveError, match="already"):
            leave_service.review_leave(request_row.id, "Rejected", reviewer="admin")

    def test_an_unknown_status_is_refused(self, app, students):  # noqa: ARG002
        request_row = self._pending(students)
        with pytest.raises(LeaveError, match="Status must be"):
            leave_service.review_leave(request_row.id, "Maybe", reviewer="admin")

    def test_reviewer_comes_from_the_session_not_the_form(self, admin_client, students):
        """The old form posted `reviewed_by` and the server stored whatever it said."""
        request_row = self._pending(students)

        admin_client.post(
            "/review_leave",
            data={
                "leave_id": request_row.id,
                "status": "Approved",
                "reviewed_by": "somebody-else",
                "review_notes": "ok",
            },
            follow_redirects=True,
        )

        db.session.refresh(request_row)
        assert request_row.reviewed_by == "admin"


class TestListing:
    def test_status_counts_are_one_query(self, app, students, query_counter):  # noqa: ARG002
        """The page previously issued four separate count() queries."""
        with query_counter as counter:
            counts = leave_service.status_counts()

        assert counter.count == 1
        assert set(counts) == {"Pending", "Approved", "Rejected"}

    def test_page_renders_with_filters(self, admin_client, students):
        admin_client.post("/apply_leave", data=form(students), follow_redirects=True)

        for query in ("", "?status=Pending", "?leave_type=Sick", "?date_from=2020-01-01"):
            assert admin_client.get(f"/leave{query}").status_code == 200

    def test_bad_date_filter_does_not_500(self, admin_client, students):  # noqa: ARG002
        assert admin_client.get("/leave?date_from=not-a-date").status_code == 200

    def test_on_leave_today_api(self, admin_client, students):
        request_row = self._approved(students)
        assert request_row.status == "Approved"

        response = admin_client.get("/api/students_on_leave")
        assert response.status_code == 200
        assert response.get_json()["count"] == 1

    def test_leave_detail_api(self, admin_client, students):
        request_row = self._approved(students)
        response = admin_client.get(f"/api/leave/{request_row.id}")
        assert response.status_code == 200
        assert response.get_json()["status"] == "Approved"

    def test_missing_leave_detail_is_a_404(self, admin_client):
        assert admin_client.get("/api/leave/999").status_code == 404

    @staticmethod
    def _approved(students) -> LeaveRequest:
        today = app_today()
        row = leave_service.apply_for_leave(
            {
                "student_id": students[0].id,
                "leave_type": "Sick",
                "start_date_parsed": today,
                "end_date_parsed": today,
                "reason": "Doctor has advised rest today.",
            }
        )
        return leave_service.review_leave(row.id, "Approved", reviewer="admin")
