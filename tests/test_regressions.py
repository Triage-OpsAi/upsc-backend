import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from app.database import _is_connection_capacity_error, _transaction_pooler_dsn
from app.routers.auth import _device_warning
from app.routers import reports
from app.schemas import ArchiveMonthOut, OtpRequestOut, OtpVerify
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
            resend_after_seconds=30,
        )
        self.assertTrue(response.account_exists)
        self.assertEqual(response.resend_after_seconds, 30)

    def test_device_limit_warning_is_visible_before_suspension(self):
        warning = _device_warning(2)
        self.assertIsNotNone(warning)
        self.assertIn("Signing in on another new device", warning)


class ReliabilityRegressionTests(unittest.TestCase):
    def test_supabase_session_pooler_is_changed_to_transaction_pooler(self):
        original = "postgresql://user:secret@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"
        converted = _transaction_pooler_dsn(original)
        self.assertIn(":6543/postgres", converted)
        self.assertNotIn(":5432/postgres", converted)

    def test_capacity_error_marker_is_retryable(self):
        error = RuntimeError("(EMAXCONNSESSION) max clients reached in session mode")
        self.assertTrue(_is_connection_capacity_error(error))

    def test_archive_month_includes_question_count(self):
        month = ArchiveMonthOut(year=2026, month=7, question_count=30)
        self.assertEqual(month.question_count, 30)


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
