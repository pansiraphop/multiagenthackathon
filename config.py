"""InstaCook configuration. Every tunable lives here — no magic numbers in stages."""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# --- credentials -----------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
# Publishable key is enough while RLS is disabled; service key overrides it.
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

GOOGLE_CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "credentials.json")
GOOGLE_TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "token.json")
GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
# One scope covering both free/busy reads and event writes. calendar.events
# alone 403s on free/busy.
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]

INSTACART_API_KEY = os.environ.get("INSTACART_API_KEY", "")
INSTACART_API_BASE = os.environ.get("INSTACART_API_BASE", "https://connect.instacart.com")

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

# --- time ------------------------------------------------------------------
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles"))

# --- cooking windows (stage 2) ---------------------------------------------
COOK_WINDOW_START = "17:30"   # no 6am cooking
COOK_WINDOW_END = "21:30"
MIN_SLOT_MINUTES = 25         # shorter gaps aren't worth surfacing
SLOT_BUFFER_MINUTES = 15      # prep/cleanup pad added to est_time_minutes

# --- planning (stage 3) ----------------------------------------------------
MEALS_PER_WEEK = 5
W_EXPIRY = 0.55               # weights should sum to 1.0
W_OVERLAP = 0.25
W_COMPLETENESS = 0.20
DIVERSITY_PENALTY = 0.60      # multiplier when the cuisine is already used

# --- delivery (stage 5) ----------------------------------------------------
DELIVERY_LEAD_HOURS = 4       # earliest realistic turnaround from ordering
DELIVERY_BUFFER_HRS = 2       # margin between delivery end and first cook slot

# --- normalization vocabulary ----------------------------------------------
UNITS = ("g", "kg", "ml", "l", "cup", "tbsp", "tsp", "unit")

CUISINES = (
    "italian", "mexican", "indian", "chinese", "japanese", "thai",
    "mediterranean", "american", "korean", "middle_eastern", "other",
)

# --- extraction escalation gate (stage 1) ----------------------------------
# Deterministic, deliberately: the branch has to be reproducible and countable.
MIN_INGREDIENTS_FOR_COMPLETE = 3
MIN_STEPS_FOR_COMPLETE = 1
EST_TIME_MIN = 5
EST_TIME_MAX = 180


def week_start(d: date | None = None) -> date:
    """Monday of the week being PLANNED. The single source of week_start_date.

    Upcoming Monday, or today if today is Monday. Deliberately not the Monday of
    the current week: running this on a Sunday would otherwise return six days
    ago, and every cook slot and calendar event would land in the past.

    Stage 2 should still filter slots to the future defensively — this function
    picks the week, it doesn't guarantee every hour in it is yet to come.
    """
    d = d or datetime.now(TIMEZONE).date()
    return d + timedelta(days=(7 - d.weekday()) % 7)


def now_local() -> datetime:
    """Timezone-aware now. Never use datetime.now() bare anywhere in this repo."""
    return datetime.now(TIMEZONE)
