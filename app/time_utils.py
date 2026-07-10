from datetime import date, datetime
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def ist_today() -> date:
    """Return the current calendar date used by the India-facing product."""
    return datetime.now(IST).date()
