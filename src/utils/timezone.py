"""Central timezone utility — Argentina time."""

from datetime import datetime
from zoneinfo import ZoneInfo

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")


def now_ar() -> datetime:
    """Return current Argentina time, tz-naive for DB storage."""
    return datetime.now(TZ_AR).replace(tzinfo=None)
