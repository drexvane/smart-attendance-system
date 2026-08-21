"""Validation and sanitisation."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.utils.validators import (
    clamp_page_size,
    normalize_email,
    sanitize_input,
    validate_leave_request_data,
    validate_student_data,
)

LEAVE_TYPES = ("Sick", "Personal", "Family", "Academic", "Other")


class TestSanitizeInput:
    def test_strips_html_tags(self):
        assert sanitize_input("<script>alert(1)</script>Bob") == "alert(1)Bob"

    def test_keeps_basic_tags_when_allowed(self):
        assert sanitize_input("<b>bold</b>", allow_basic_html=True) == "<b>bold</b>"

    def test_drops_tags_not_on_the_allow_list(self):
        assert "<img" not in sanitize_input("<img src=x onerror=alert(1)>", allow_basic_html=True)

    @pytest.mark.parametrize("falsy", [None, "", 0])
    def test_falsy_values_pass_through_unchanged(self, falsy):
        """Returning str(None) would silently write "None" into the database."""
        assert sanitize_input(falsy) == falsy


class TestNormalizeEmail:
    def test_accepts_a_real_address(self):
        normalised, error = normalize_email("Bob@School.EDU")
        assert error is None
        assert normalised == "Bob@school.edu"

    @pytest.mark.parametrize("bad", ["@", "no-at-sign", "a@", "@school.edu", "a b@school.edu"])
    def test_rejects_malformed_addresses(self, bad):
        """The old check was `'@' not in email`, which accepted "@" by itself."""
        normalised, error = normalize_email(bad)
        assert normalised is None
        assert error


class TestValidateStudentData:
    @staticmethod
    def payload(**overrides):
        data = {
            "student_id": "S001",
            "name": "Alice Example",
            "email": "alice@school.edu",
            "phone": "9876543210",
            "department": "CSE",
            "year": "3",
            "section": "A",
        }
        data.update(overrides)
        return data

    def test_accepts_a_valid_payload(self):
        errors, cleaned = validate_student_data(self.payload())
        assert errors == []
        assert cleaned["email"] == "alice@school.edu"

    @pytest.mark.parametrize(
        "field", ["student_id", "name", "email", "department", "year", "section"]
    )
    def test_required_fields_are_reported(self, field):
        errors, _ = validate_student_data(self.payload(**{field: ""}))
        assert any(field.replace("_", " ").title() in error for error in errors)

    @pytest.mark.parametrize("bad_id", ["ab", "has space", "x" * 21, "<b>S1</b>"])
    def test_rejects_malformed_student_ids(self, bad_id):
        errors, _ = validate_student_data(self.payload(student_id=bad_id))
        assert errors

    def test_rejects_a_one_character_name(self):
        errors, _ = validate_student_data(self.payload(name="A"))
        assert any("Name must be" in error for error in errors)

    def test_strips_html_from_the_name(self):
        _, cleaned = validate_student_data(self.payload(name="<script>x</script>Alice"))
        assert "<" not in cleaned["name"]

    def test_rejects_a_nonsense_phone_number(self):
        errors, _ = validate_student_data(self.payload(phone="not-a-phone-number-at-all"))
        assert any("Phone" in error for error in errors)


class TestValidateLeaveRequestData:
    @staticmethod
    def payload(today, **overrides):
        data = {
            "student_id": "1",
            "leave_type": "Sick",
            "start_date": today.isoformat(),
            "end_date": (today + timedelta(days=2)).isoformat(),
            "reason": "Doctor has advised bed rest for a few days.",
        }
        data.update(overrides)
        return data

    def test_accepts_a_valid_request(self):
        today = date(2026, 8, 21)
        errors, cleaned = validate_leave_request_data(
            self.payload(today), LEAVE_TYPES, today_value=today
        )
        assert errors == []
        assert cleaned["start_date_parsed"] == today

    def test_rejects_an_end_date_before_the_start(self):
        today = date(2026, 8, 21)
        errors, _ = validate_leave_request_data(
            self.payload(today, end_date=(today - timedelta(days=1)).isoformat()),
            LEAVE_TYPES,
            today_value=today,
        )
        assert any("End date cannot be before" in error for error in errors)

    def test_rejects_a_short_reason(self):
        today = date(2026, 8, 21)
        errors, _ = validate_leave_request_data(
            self.payload(today, reason="sick"), LEAVE_TYPES, today_value=today
        )
        assert any("at least" in error for error in errors)

    def test_rejects_an_unknown_leave_type(self):
        today = date(2026, 8, 21)
        errors, _ = validate_leave_request_data(
            self.payload(today, leave_type="Vacation"), LEAVE_TYPES, today_value=today
        )
        assert any("Leave type must be" in error for error in errors)

    def test_rejects_a_malformed_date(self):
        today = date(2026, 8, 21)
        errors, _ = validate_leave_request_data(
            self.payload(today, start_date="21/08/2026"), LEAVE_TYPES, today_value=today
        )
        assert any("Invalid start date" in error for error in errors)

    def test_rejects_backdating_beyond_a_week(self):
        today = date(2026, 8, 21)
        errors, _ = validate_leave_request_data(
            self.payload(today, start_date=(today - timedelta(days=30)).isoformat()),
            LEAVE_TYPES,
            today_value=today,
        )
        assert any("more than 7 days in the past" in error for error in errors)


class TestClampPageSize:
    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            (None, 50),
            (0, 50),
            (-10, 50),  # the old code passed this straight to paginate()
            (10, 10),
            (5000, 500),
        ],
    )
    def test_bounds_the_requested_size(self, requested, expected):
        assert clamp_page_size(requested, default=50, maximum=500) == expected
