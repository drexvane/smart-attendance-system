"""Analytics queries.

Every endpoint here used to loop in Python and issue one query per iteration:

* ``/api/analytics/trend?days=30``      -> 1 + 30 queries
* ``/api/analytics/top_students``       -> 1 + one query *per student* (501 for 500)
* ``/api/analytics/at_risk``            -> the same again
* ``/api/analytics/department``         -> 1 + 2 per department
* ``/api/analytics/weekly_heatmap``     -> 1 + 7 per week

They are now aggregate ``GROUP BY`` queries with a fixed cost, and the per-date gap
filling happens once in Python over the result set.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta

from sqlalchemy import and_, case, func, literal

from app.extensions import db
from app.models import AttendanceRecord, LeaveRequest, Student

PRESENT_STATUSES = ("Present", "Late")


@dataclass(frozen=True)
class DateRange:
    start: date_type
    end: date_type

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def each_day(self):
        current = self.start
        while current <= self.end:
            yield current
            current += timedelta(days=1)


def active_student_count() -> int:
    return (
        db.session.execute(
            db.select(func.count()).select_from(Student).where(Student.is_active.is_(True))
        ).scalar_one()
        or 0
    )


def status_counts_by_date(window: DateRange) -> dict[date_type, dict[str, int]]:
    """``{date: {status: count}}`` for the window, in one query."""
    rows = db.session.execute(
        db.select(
            AttendanceRecord.date,
            AttendanceRecord.status,
            func.count().label("total"),
        )
        .where(AttendanceRecord.date >= window.start, AttendanceRecord.date <= window.end)
        .group_by(AttendanceRecord.date, AttendanceRecord.status)
    ).all()

    result: dict[date_type, dict[str, int]] = defaultdict(dict)
    for day, status, total in rows:
        result[day][status] = total
    return result


def trend(window: DateRange) -> dict:
    """Daily present/absent/late/on-leave counts and an attendance rate."""
    counts = status_counts_by_date(window)
    roster = active_student_count()

    series = []
    for day in window.each_day():
        day_counts = counts.get(day, {})
        present = day_counts.get("Present", 0)
        late = day_counts.get("Late", 0)
        series.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%b %d"),
                "present": present,
                "absent": day_counts.get("Absent", 0),
                "late": late,
                "on_leave": day_counts.get("On Leave", 0),
                "rate": _rate(present + late, roster),
            }
        )
    return {"trend": series, "total_students": roster}


def status_distribution(window: DateRange) -> dict:
    """Total records per status across the window, in one query."""
    rows = db.session.execute(
        db.select(AttendanceRecord.status, func.count())
        .where(AttendanceRecord.date >= window.start, AttendanceRecord.date <= window.end)
        .group_by(AttendanceRecord.status)
    ).all()
    distribution = {"Present": 0, "Absent": 0, "Late": 0, "On Leave": 0, "Excused": 0}
    for status, total in rows:
        distribution[status] = distribution.get(status, 0) + total
    return {"distribution": distribution}


def department_stats(window: DateRange) -> dict:
    """Per-department roster size and attendance rate, in two queries."""
    roster_rows = db.session.execute(
        db.select(Student.department, func.count())
        .where(Student.is_active.is_(True), Student.department.isnot(None))
        .group_by(Student.department)
    ).all()

    present_rows = db.session.execute(
        db.select(
            Student.department,
            func.sum(_present_case()).label("present"),
            func.count(AttendanceRecord.id).label("records"),
        )
        .join(AttendanceRecord, AttendanceRecord.student_id == Student.id)
        .where(
            Student.is_active.is_(True),
            Student.department.isnot(None),
            AttendanceRecord.date >= window.start,
            AttendanceRecord.date <= window.end,
        )
        .group_by(Student.department)
    ).all()

    present_by_dept = {
        dept: (int(present or 0), records) for dept, present, records in present_rows
    }

    departments = []
    for dept, roster in roster_rows:
        if not dept:
            continue
        present, records = present_by_dept.get(dept, (0, 0))
        departments.append(
            {
                "department": dept,
                "students": roster,
                "present": present,
                "records": records,
                "rate": _rate(present, roster * window.days),
            }
        )
    departments.sort(key=lambda item: item["rate"], reverse=True)
    return {"departments": departments}


def student_attendance_rates(window: DateRange) -> list[dict]:
    """Per-student present-day count and rate for the window, in one query.

    A LEFT OUTER JOIN with the date filter in the ``ON`` clause (not ``WHERE``) keeps
    students who have no records at all -- they are exactly the ones an at-risk report
    needs to surface, and the old per-student loop dropped them to a 0% row only by
    accident.
    """
    rows = db.session.execute(
        db.select(
            Student.id,
            Student.student_id,
            Student.name,
            Student.department,
            func.coalesce(func.sum(_present_case()), 0).label("present_days"),
        )
        .outerjoin(
            AttendanceRecord,
            and_(
                AttendanceRecord.student_id == Student.id,
                AttendanceRecord.date >= window.start,
                AttendanceRecord.date <= window.end,
            ),
        )
        .where(Student.is_active.is_(True))
        .group_by(Student.id, Student.student_id, Student.name, Student.department)
    ).all()

    return [
        {
            "id": pk,
            "student_id": roll,
            "name": name,
            "department": department,
            "present_days": int(present or 0),
            "rate": _rate(int(present or 0), window.days),
        }
        for pk, roll, name, department, present in rows
    ]


def top_students(window: DateRange, limit: int = 10) -> dict:
    ranked = sorted(
        student_attendance_rates(window),
        key=lambda item: (item["rate"], item["present_days"]),
        reverse=True,
    )
    return {"top_students": ranked[:limit]}


def at_risk_students(window: DateRange, threshold: float = 75.0, limit: int = 10) -> dict:
    below = [row for row in student_attendance_rates(window) if row["rate"] < threshold]
    below.sort(key=lambda item: item["rate"])
    return {"at_risk": below[:limit], "threshold": threshold, "total_at_risk": len(below)}


def weekly_heatmap(weeks: int, reference: date_type) -> dict:
    """Attendance rate per weekday for the last ``weeks`` weeks, in one query."""
    week_start = reference - timedelta(days=reference.weekday() + (weeks - 1) * 7)
    window = DateRange(start=week_start, end=reference)
    counts = status_counts_by_date(window)
    roster = active_student_count()

    heatmap = []
    for index in range(weeks):
        start = week_start + timedelta(days=index * 7)
        days: list[dict[str, object]] = []
        for offset in range(7):
            day = start + timedelta(days=offset)
            if day > reference:
                days.append({"day": day.strftime("%a"), "date": day.isoformat(), "rate": None})
                continue
            day_counts = counts.get(day, {})
            present = day_counts.get("Present", 0) + day_counts.get("Late", 0)
            days.append(
                {"day": day.strftime("%a"), "date": day.isoformat(), "rate": _rate(present, roster)}
            )
        heatmap.append({"week": f"Week {index + 1}", "start": start.isoformat(), "days": days})
    return {"heatmap": heatmap}


def recent_activity(limit: int = 20) -> dict:
    """Latest records, eager-loading the student to avoid a lazy load per row."""
    records = (
        db.session.execute(
            db.select(AttendanceRecord)
            .options(db.joinedload(AttendanceRecord.student))
            .order_by(AttendanceRecord.created_at.desc())
            .limit(limit)
        )
        .unique()
        .scalars()
        .all()
    )
    return {
        "activity": [
            {
                "student_name": record.student.name if record.student else "Unknown",
                "student_id": record.student.student_id if record.student else "N/A",
                "status": record.status,
                "date": record.date.isoformat(),
                "time": record.time_in.isoformat() if record.time_in else None,
                "marked_by": record.marked_by,
            }
            for record in records
        ]
    }


def overview(reference: date_type, window: DateRange) -> dict:
    """Headline numbers for the analytics dashboard."""
    roster = active_student_count()
    counts = status_counts_by_date(DateRange(start=window.start, end=reference))

    today_counts = counts.get(reference, {})
    today_present = today_counts.get("Present", 0) + today_counts.get("Late", 0)

    week_start = reference - timedelta(days=6)
    week_present = sum(
        day_counts.get("Present", 0) + day_counts.get("Late", 0)
        for day, day_counts in counts.items()
        if week_start <= day <= reference
    )
    month_present = sum(
        day_counts.get("Present", 0) + day_counts.get("Late", 0) for day_counts in counts.values()
    )

    on_leave_today = db.session.execute(
        db.select(func.count())
        .select_from(LeaveRequest)
        .where(
            LeaveRequest.status == "Approved",
            LeaveRequest.start_date <= reference,
            LeaveRequest.end_date >= reference,
        )
    ).scalar_one()
    pending_leaves = db.session.execute(
        db.select(func.count()).select_from(LeaveRequest).where(LeaveRequest.status == "Pending")
    ).scalar_one()

    return {
        "total_students": roster,
        "today_rate": _rate(today_present, roster),
        "week_avg": _rate(week_present, roster * 7),
        "month_avg": _rate(month_present, roster * window.days),
        "on_leave_today": on_leave_today,
        "pending_leaves": pending_leaves,
    }


def summarise(window: DateRange) -> dict:
    """Counts and percentages for the reports page, in one query.

    Replaces ``generate_attendance_summary``, which loaded every record in the range
    into memory and iterated it four times.
    """
    rows = db.session.execute(
        db.select(AttendanceRecord.status, func.count())
        .where(AttendanceRecord.date >= window.start, AttendanceRecord.date <= window.end)
        .group_by(AttendanceRecord.status)
    ).all()
    counts: dict[str, int] = dict(rows)  # type: ignore[arg-type]
    total = sum(counts.values())

    def pct(value: int) -> float:
        return round(value / total * 100, 1) if total else 0.0

    present = counts.get("Present", 0)
    absent = counts.get("Absent", 0)
    late = counts.get("Late", 0)
    on_leave = counts.get("On Leave", 0)
    return {
        "total_records": total,
        "present_count": present,
        "absent_count": absent,
        "late_count": late,
        "on_leave_count": on_leave,
        "present_percentage": pct(present),
        "absent_percentage": pct(absent),
        "late_percentage": pct(late),
        "on_leave_percentage": pct(on_leave),
    }


def _present_case():
    """SQL ``SUM(CASE WHEN status IN ('Present','Late') THEN 1 ELSE 0 END)`` term."""
    return case((AttendanceRecord.status.in_(PRESENT_STATUSES), literal(1)), else_=literal(0))


def _rate(numerator: int, denominator: int) -> float:
    """Percentage, clamped so a partially-populated roster cannot exceed 100."""
    if denominator <= 0:
        return 0.0
    return round(min(numerator / denominator, 1.0) * 100, 1)
