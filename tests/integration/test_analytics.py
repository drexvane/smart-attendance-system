"""Analytics: correctness and, crucially, query count.

Each of these endpoints previously issued one query per student or per day, so the cost
grew with the data:

* ``/api/analytics/top_students`` -- 1 + N queries (501 for 500 students)
* ``/api/analytics/at_risk``      -- 1 + N
* ``/api/analytics/trend?days=30`` -- 1 + 30
* ``/api/analytics/department``   -- 1 + 2 per department
* ``/api/analytics/weekly_heatmap?weeks=4`` -- 1 + 28

The tests below assert a fixed ceiling that does not move when rows are added, which is
the property that actually matters.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import AttendanceRecord, Student
from app.services import analytics_service
from app.services.analytics_service import DateRange
from tests.conftest import app_today


@pytest.fixture
def window():
    today = app_today()
    return DateRange(start=today - timedelta(days=29), end=today)


@pytest.fixture
def many_students(db):
    """Enough students that a per-student query pattern would be obvious."""
    rows = [
        Student(
            student_id=f"B{index:04d}",
            name=f"Bulk {index}",
            email=f"bulk{index}@school.edu",
            department=f"DEPT{index % 4}",
            year=str(index % 4 + 1),
            section="A",
        )
        for index in range(40)
    ]
    db.session.add_all(rows)
    db.session.commit()

    today = app_today()
    records = []
    for offset in range(10):
        day = today - timedelta(days=offset)
        for index, student in enumerate(rows):
            records.append(
                AttendanceRecord(
                    student_id=student.id,
                    date=day,
                    status="Present" if (index + offset) % 3 else "Absent",
                    marked_by="fixture",
                )
            )
    db.session.add_all(records)
    db.session.commit()
    return rows


class TestQueryCountIsIndependentOfRowCount:
    def test_student_rates_is_one_query(self, app, many_students, query_counter):  # noqa: ARG002
        with query_counter as counter:
            rows = analytics_service.student_attendance_rates(
                DateRange(start=app_today() - timedelta(days=9), end=app_today())
            )

        assert len(rows) == 40
        assert counter.count == 1, f"expected 1 query, got {counter.count}"

    def test_top_students_is_one_query(self, app, many_students, query_counter, window):  # noqa: ARG002
        with query_counter as counter:
            analytics_service.top_students(window, limit=5)
        assert counter.count == 1

    def test_at_risk_is_one_query(self, app, many_students, query_counter, window):  # noqa: ARG002
        with query_counter as counter:
            analytics_service.at_risk_students(window, threshold=75, limit=5)
        assert counter.count == 1

    def test_trend_over_thirty_days_is_two_queries(self, app, many_students, query_counter, window):  # noqa: ARG002
        """One aggregate for the records, one for the roster size -- not one per day."""
        with query_counter as counter:
            result = analytics_service.trend(window)

        assert len(result["trend"]) == 30
        assert counter.count <= 2, f"expected <=2 queries for 30 days, got {counter.count}"

    def test_status_distribution_is_one_query(self, app, many_students, query_counter, window):  # noqa: ARG002
        with query_counter as counter:
            analytics_service.status_distribution(window)
        assert counter.count == 1

    def test_department_stats_is_two_queries(self, app, many_students, query_counter, window):  # noqa: ARG002
        with query_counter as counter:
            result = analytics_service.department_stats(window)

        assert len(result["departments"]) == 4
        assert counter.count == 2

    def test_weekly_heatmap_is_two_queries(self, app, many_students, query_counter):  # noqa: ARG002
        with query_counter as counter:
            result = analytics_service.weekly_heatmap(4, app_today())

        assert len(result["heatmap"]) == 4
        assert counter.count <= 2

    def test_summarise_is_one_query(self, app, many_students, query_counter, window):  # noqa: ARG002
        with query_counter as counter:
            analytics_service.summarise(window)
        assert counter.count == 1

    def test_recent_activity_is_one_query(self, app, many_students, query_counter):  # noqa: ARG002
        """joinedload avoids a lazy load of the student per row."""
        with query_counter as counter:
            result = analytics_service.recent_activity(limit=20)

        assert len(result["activity"]) == 20
        assert counter.count == 1

    def test_doubling_the_students_does_not_change_the_query_count(
        self, app, db, many_students, query_counter, window
    ):  # noqa: ARG002
        with query_counter as counter:
            analytics_service.top_students(window)
        before = counter.count

        db.session.add_all(
            [
                Student(
                    student_id=f"X{index:04d}",
                    name=f"Extra {index}",
                    email=f"extra{index}@school.edu",
                    department="DEPT0",
                    year="1",
                    section="B",
                )
                for index in range(40)
            ]
        )
        db.session.commit()

        with query_counter as counter:
            analytics_service.top_students(window)

        assert counter.count == before


class TestCorrectness:
    def test_rates_reflect_the_data(self, app, students, attendance_history):  # noqa: ARG002
        rows = {
            row["student_id"]: row
            for row in analytics_service.student_attendance_rates(
                DateRange(start=app_today() - timedelta(days=4), end=app_today())
            )
        }
        # Fixture: student 0 present every day, student 2 absent every day.
        assert rows["S000"]["present_days"] == 5
        assert rows["S002"]["present_days"] == 0

    def test_late_counts_as_present_for_rate_purposes(self, app, students, db):
        db.session.add(
            AttendanceRecord(
                student_id=students[0].id, date=app_today(), status="Late", marked_by="t"
            )
        )
        db.session.commit()

        rows = analytics_service.student_attendance_rates(
            DateRange(start=app_today(), end=app_today())
        )
        assert next(r for r in rows if r["student_id"] == "S000")["present_days"] == 1

    def test_students_with_no_records_still_appear(self, app, students):  # noqa: ARG002
        """An at-risk report is useless if it silently drops the worst cases."""
        rows = analytics_service.student_attendance_rates(
            DateRange(start=app_today(), end=app_today())
        )
        assert len(rows) == 3
        assert all(row["rate"] == 0.0 for row in rows)

    def test_rate_never_exceeds_one_hundred(self, app, students, db):
        for offset in range(3):
            db.session.add(
                AttendanceRecord(
                    student_id=students[0].id,
                    date=app_today() - timedelta(days=offset),
                    status="Present",
                    marked_by="t",
                )
            )
        db.session.commit()

        rows = analytics_service.student_attendance_rates(
            DateRange(start=app_today(), end=app_today())
        )
        assert all(row["rate"] <= 100.0 for row in rows)

    def test_trend_fills_days_with_no_records(self, app, students):  # noqa: ARG002
        result = analytics_service.trend(
            DateRange(start=app_today() - timedelta(days=6), end=app_today())
        )
        assert len(result["trend"]) == 7
        assert all(day["present"] == 0 for day in result["trend"])

    def test_empty_database_does_not_divide_by_zero(self, app):  # noqa: ARG002
        result = analytics_service.trend(DateRange(start=app_today(), end=app_today()))
        assert result["total_students"] == 0
        assert result["trend"][0]["rate"] == 0.0


class TestEndpoints:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/analytics/trend",
            "/api/analytics/department",
            "/api/analytics/status_distribution",
            "/api/analytics/top_students",
            "/api/analytics/at_risk",
            "/api/analytics/weekly_heatmap",
            "/api/analytics/recent_activity",
        ],
    )
    def test_returns_json(self, admin_client, attendance_history, path):  # noqa: ARG002
        response = admin_client.get(path)
        assert response.status_code == 200
        assert response.is_json

    @pytest.mark.parametrize("days", ["abc", "-5", "0", "999999", ""])
    def test_bad_days_parameter_does_not_500(self, admin_client, days):
        """`int(request.args.get('days', 30))` raised a ValueError on 'abc'."""
        assert admin_client.get(f"/api/analytics/trend?days={days}").status_code == 200

    def test_days_is_capped(self, admin_client, query_counter):
        with query_counter as counter:
            response = admin_client.get("/api/analytics/trend?days=100000")
        assert response.status_code == 200
        assert len(response.get_json()["trend"]) <= 365
        assert counter.count < 10

    def test_threshold_is_clamped(self, admin_client, attendance_history):  # noqa: ARG002
        response = admin_client.get("/api/analytics/at_risk?threshold=99999")
        assert response.status_code == 200
        assert response.get_json()["threshold"] == 100.0

    def test_limit_is_capped(self, admin_client, attendance_history):  # noqa: ARG002
        response = admin_client.get("/api/analytics/top_students?limit=100000")
        assert response.status_code == 200
        assert len(response.get_json()["top_students"]) <= 200
