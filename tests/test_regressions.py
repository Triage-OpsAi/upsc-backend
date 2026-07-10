import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from app.routers import reports
from app.schemas import OtpRequestOut, OtpVerify
from app.services.report_generator import _personalized_feedback


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _LatestAttemptConnection:
    def __init__(self):
        self.fetch_count = 0

    async def fetchrow(self, query, *args):
        self.fetch_count += 1
        if self.fetch_count == 1:
            return {
                "report_date": date(2026, 7, 8),
                "attempt_date": date(2026, 7, 9),
            }
        return None


class AuthRegressionTests(unittest.TestCase):
    def test_existing_login_profile_fields_are_optional(self):
        payload = OtpVerify(
            email="returning@example.com",
            otp="123456",
            device_id="device-id-123",
        )
        self.assertIsNone(payload.name)
        self.assertIsNone(payload.target_exam)

    def test_otp_response_identifies_existing_account(self):
        response = OtpRequestOut(
            ok=True,
            expires_in_minutes=10,
            account_exists=True,
        )
        self.assertTrue(response.account_exists)


class ReportRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_date_uses_latest_attempt_when_newer_than_saved_report(self):
        conn = _LatestAttemptConnection()
        live_report = {
            "report_date": date(2026, 7, 9),
            "total_attempted": 3,
            "total_correct": 2,
            "accuracy": 66.67,
            "percentile": 75.0,
            "subject_breakdown": {"polity": {"total": 3, "correct": 2}},
            "exam_wise_readiness": {"UPSC": 66.7},
            "ai_feedback": "Focus on polity.",
        }
        builder = AsyncMock(return_value=live_report)

        with (
            patch.object(reports, "acquire", return_value=_AcquireContext(conn)),
            patch.object(reports, "build_report_for_student", builder),
        ):
            result = await reports._get_report_for_student("student-1", None)

        self.assertEqual(result.report_date, date(2026, 7, 9))
        self.assertEqual(result.total_attempted, 3)
        builder.assert_awaited_once_with(conn, "student-1", date(2026, 7, 9))

    def test_fallback_feedback_is_personalized(self):
        feedback = _personalized_feedback(
            "UPSC Prelims",
            {
                "total_attempted": 3,
                "accuracy": 66.67,
                "subject_breakdown": {
                    "polity": {"total": 2, "correct": 2},
                    "economy": {"total": 1, "correct": 0},
                },
            },
        )
        self.assertIn("UPSC Prelims", feedback)
        self.assertIn("polity", feedback)
        self.assertIn("economy", feedback)


if __name__ == "__main__":
    unittest.main()
