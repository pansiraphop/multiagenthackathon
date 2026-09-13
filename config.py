"""InstaCook configuration. Every tunable lives here — no magic numbers in stages."""

import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# The terminal narration IS the demo, and model-generated text (score_reason)
# contains em-dashes and curly quotes. The Windows console defaults to cp1252
# and renders those as replacement characters, so force UTF-8 on the way out.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


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
BROWSERBASE_API_KEY = os.environ.get("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID = os.environ.get("BROWSERBASE_PROJECT_ID", "")
BROWSERBASE_CONTEXT_ID = os.environ.get("BROWSERBASE_CONTEXT_ID", "")
INSTACART_ZIP = os.environ.get("INSTACART_ZIP", "")
INSTACART_RETAILER = os.environ.get("INSTACART_RETAILER", "")

APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "")
ELEVENLABS_API_BASE = os.environ.get("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")
# Rachel, on every ElevenLabs account by default. Override with a voice id or
# a voice name from `python -m stages.voiceover --voices`.
ELEVENLABS_VOICE = os.environ.get("ELEVENLABS_VOICE", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
ELEVENLABS_OUTPUT_FORMAT = os.environ.get("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128")
ELEVENLABS_TIMEOUT_SECONDS = _env_int("ELEVENLABS_TIMEOUT_SECONDS", 120)

# Instagram webhook ingestion. Caption is always primary; local Whisper is
# best-effort and may be switched off without disabling the webhook.
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN", "")
NGROK_URL = os.environ.get("NGROK_URL", "").rstrip("/")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or (
    f"{NGROK_URL}/webhook" if NGROK_URL else ""
)
INGEST_TRANSCRIBE = _env_bool("INGEST_TRANSCRIBE", True)
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")

# --- time ------------------------------------------------------------------
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles"))

# Marks every calendar event InstaCook creates. Stage 2 subtracts these from
# busy time (otherwise our own meals shrink next week's windows and the plan
# drifts on every re-run) and stage 6 uses it to find what to clear.
CALENDAR_MARKER = "[instacook]"

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
# Bonus for a recipe whose ingredients are already needed by a meal picked
# earlier this week: one bunch of coriander across two dinners costs less than
# two half-used bunches. Applied against the fraction shared, so it nudges
# rather than overrides expiry urgency.
W_SHARED_INGREDIENTS = 0.20

# --- delivery (stage 5) ----------------------------------------------------
# One cart per week. Stage 4 keeps this status on re-run so an ingredient that
# is already in the cart is never ordered a second time.
CART_READY_STATUS = "added_to_cart"
DELIVERY_LEAD_HOURS = 4       # earliest realistic turnaround from ordering
DELIVERY_BUFFER_HRS = 2       # margin between delivery end and first cook slot
BROWSER_SELECTOR_TIMEOUT_MS = 8000
BROWSER_NAV_TIMEOUT_MS = 20000
BROWSER_ITEM_RETRIES = 1
BROWSER_SESSION_TIMEOUT_SECONDS = _env_int(
    "BROWSERBASE_SESSION_TIMEOUT_SECONDS", 1800
)
FAILURE_SHOT_DIR = "failures"

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
EST_TIME_MAX = 180            # active hands-on work
TOTAL_TIME_MAX = 600          # attended span: a 5-hour braise is real
MAX_ADVANCE_PREP_MINUTES = 4320   # 3 days covers brining and long ferments

# --- voiceover (stage 7) ---------------------------------------------------
VOICEOVER_DIR = os.environ.get("VOICEOVER_DIR", "out/voiceover")
# Measured on narration, not on reading speed: a demo voice that races is
# worse than a long one, so the estimate is deliberately conservative.
SPOKEN_WORDS_PER_MINUTE = 150
# The demo is two minutes and the voiceover is not allowed to eat all of it.
WEEK_SCRIPT_TARGET_WORDS = 220
WEEK_SCRIPT_MAX_WORDS = 320
# A cook-along is read while someone is standing at a stove, so it runs long
# by design — one step at a time, with the ingredients read out first.
COOK_SCRIPT_MAX_WORDS = 700
# eleven_multilingual_v2 rejects requests past ~5k characters. Split below it
# and stitch the MP3s rather than silently truncating a sentence.
TTS_MAX_CHARS = 4500


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
