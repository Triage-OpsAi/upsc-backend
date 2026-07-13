from pydantic import BaseModel, Field
from typing import Optional, Any
from datetime import date, datetime


class PageMeta(BaseModel):
    page: int
    page_size: int
    total_items: int
    total_pages: int


class TopicOut(BaseModel):
    id: str
    month: int
    year: int
    title: str
    summary: Optional[str]
    subject_tags: list[str]
    source_date: Optional[date]
    question_text: Optional[str] = None


class TopicListOut(BaseModel):
    items: list[TopicOut]
    meta: PageMeta


class NextTopicOut(BaseModel):
    topic: Optional[TopicOut] = None


class ArchiveMonthOut(BaseModel):
    year: int
    month: int
    question_count: int


class QuestionOut(BaseModel):
    id: str
    topic_id: str
    question_text: str
    options: list[dict]
    difficulty: str
    # correct_option intentionally omitted - never send the answer to the client


class StudentCreate(BaseModel):
    device_id: str = Field(min_length=8, max_length=128)
    name: Optional[str] = None
    email: Optional[str] = None
    target_exam: Optional[str] = "UPSC"


class StudentOut(BaseModel):
    id: str
    device_id: str
    name: Optional[str]
    email: Optional[str] = None
    target_exam: str
    avatar_url: Optional[str] = None
    bio: Optional[str] = None
    city: Optional[str] = None
    suspended_until: Optional[datetime] = None
    recent_device_count: int = 0
    device_limit: int = 2
    device_warning: Optional[str] = None


class OtpRequest(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    purpose: str = "login"


class OtpVerify(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    otp: str = Field(min_length=4, max_length=12)
    device_id: str = Field(min_length=8, max_length=128)
    name: Optional[str] = Field(default=None, max_length=120)
    # Existing users do not send profile fields while signing in. Keeping this
    # nullable also prevents a login from resetting their saved exam target.
    target_exam: Optional[str] = Field(default=None, max_length=80)


class AuthTokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str
    student: StudentOut


class OtpRequestOut(BaseModel):
    ok: bool
    expires_in_minutes: int
    account_exists: bool
    resend_after_seconds: int
    dev_otp: Optional[str] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    target_exam: Optional[str] = Field(default=None, max_length=80)
    avatar_url: Optional[str] = Field(default=None, max_length=500)
    bio: Optional[str] = Field(default=None, max_length=500)
    city: Optional[str] = Field(default=None, max_length=120)


class AttemptCreate(BaseModel):
    student_id: str
    question_id: str
    selected_option: str
    attempt_number: int = 1
    went_through_breakdown: bool = False


class AttemptResult(BaseModel):
    is_correct: bool
    correct_option: Optional[str] = None
    explanation: Optional[str] = None
    breakdown_available: bool = False


class BreakdownSlideOut(BaseModel):
    id: str
    slide_order: int
    slide_type: str
    subject: str
    content: Optional[str] = None
    practice_question: Optional[str] = None
    practice_options: Optional[list[dict]] = None
    # practice_correct_option withheld until answer submitted


class BreakdownAnswerCreate(BaseModel):
    student_id: str
    slide_id: str
    selected_option: str


class BreakdownAnswerResult(BaseModel):
    is_correct: bool
    correct_option: str
    explanation: Optional[str] = None


class DailyReportOut(BaseModel):
    report_date: date
    total_attempted: int
    total_correct: int
    accuracy: float
    percentile: float
    subject_breakdown: dict[str, Any]
    concept_breakdown: dict[str, Any] = Field(default_factory=dict)
    practice_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    exam_wise_readiness: dict[str, Any]
    ai_feedback: Optional[str]
    content_changed: bool = False
    content_change_notice: Optional[str] = None


class DashboardStatsOut(BaseModel):
    run_date: date
    questions_attempted_today: int
    correct_today: int
    accuracy_today: float
    current_streak_days: int
    rank_today: Optional[int] = None
    active_aspirants_today: int
