# InstaCook

**Turn saved Instagram recipe reels into a cooked week.**

InstaCook reads a recipe reel, checks what's already in your fridge and what's about to
expire, looks at your real Google Calendar to find the evenings you're actually free, and
then schedules the week: groceries ordered through Instacart timed to arrive before you
cook, and a calendar event for every meal with the ingredients, the steps, and the cart
link already in it.

Built for the **Multi-App AI Agent Hackathon** (Sunday, September 13, 2026).

**Apps it acts across:** Instagram · Google Calendar (read + write) · Instacart

---

## Contents

1. [Pipeline](#1-pipeline)
2. [Repo layout](#2-repo-layout)
3. [Environment & config](#3-environment--config)
4. [Database schema](#4-database-schema)
5. [Shared libraries](#5-shared-libraries)
6. [Stage 1 — `extract.py`](#6-stage-1--extractpy)
7. [Stage 2 — `availability.py`](#7-stage-2--availabilitypy)
8. [Stage 3 — `plan.py`](#8-stage-3--planpy)
9. [Stage 4 — `shopping_list.py`](#9-stage-4--shopping_listpy)
10. [Stage 5 — `instacart.py`](#10-stage-5--instacartpy)
11. [Stage 6 — `calendar_sync.py`](#11-stage-6--calendar_syncpy)
12. [Orchestrator — `run_week.py`](#12-orchestrator--run_weekpy)
13. [Evaluation — `run_eval.py`](#13-evaluation--run_evalpy)
14. [Seed data](#14-seed-data)
15. [Ownership split](#15-ownership-split)
16. [Timeline & cut list](#16-timeline--cut-list)
17. [Invariants](#17-invariants)
18. [Demo script](#18-demo-script)

---

## 1. Pipeline

```
Instagram Reel (link → caption + transcript)
        ↓
[1. extract.py]        LLM, structured output → recipes + recipe_ingredients
        ↓
[2. availability.py]   Google Calendar free/busy (READ) → cook_slots
        ↓
[3. plan.py]           deterministic scoring + constrained slot assignment → meal_plan
        ↓                                              ↑
[4. shopping_list.py]  pantry-differenced, deduped     │ re-plan if delivery
        ↓                                              │ can't arrive in time
[5. instacart.py]      cart URL + delivery window ─────┘
        ↓
[6. calendar_sync.py]  Google Calendar (WRITE) → meal events + delivery event
```

Six stages. Each is a standalone script with one job. **Stages never import each other** —
they communicate only through Supabase rows. That means either developer can run their
stage against hand-inserted rows without waiting on upstream code.

Every stage writes to `eval_log` on both success and failure, with retry count and
latency. That table is the source of the reliability brief.

### Why availability runs before planning

Reading the calendar is upstream of the planner, not downstream. The planner isn't just
ranking recipes — it solves a constrained assignment: a 45-minute recipe cannot go into a
25-minute gap. `est_time_minutes` is a hard constraint, not a display field.

---

## 2. Repo layout

```
instacook/
├── README.md                  ← this spec
├── requirements.txt
├── .env.example
├── config.py                  all tunables, no magic numbers elsewhere
├── sql/
│   └── schema.sql             run once in the Supabase SQL editor
├── lib/
│   ├── db.py                  Supabase client + row helpers
│   ├── normalize.py           normalize_ingredient() — shared by both paths
│   ├── llm.py                 call_llm_structured() wrapper
│   ├── external.py            call_external_api() wrapper
│   └── evals.py               log_eval() + report queries
├── stages/
│   ├── extract.py
│   ├── availability.py
│   ├── plan.py
│   ├── shopping_list.py
│   ├── instacart.py
│   └── calendar_sync.py
├── seed/
│   ├── seed_pantry.py
│   ├── seed_calendar.py
│   └── captions/              5–8 .txt reel captions (the eval test set)
├── tests/
│   └── ground_truth/          hand-labelled ingredient lists for 3 captions
├── run_week.py                runs stages 1–6 in order
└── run_eval.py                runs the test set, prints the reliability table
```

**Name the calendar stage `calendar_sync.py`, not `calendar.py`.** A module named
`calendar.py` shadows the Python standard library `calendar` module and will break
`google-api-python-client` imports in a way that takes 20 minutes to diagnose.

### requirements.txt

```
anthropic
supabase
python-dotenv
google-api-python-client
google-auth
google-auth-oauthlib
requests
yt-dlp
```

---

## 3. Environment & config

### `.env.example` — commit this immediately

```bash
# Supabase
SUPABASE_URL=
SUPABASE_SERVICE_KEY=

# Anthropic
ANTHROPIC_API_KEY=

# Google — OAuth client downloaded from Cloud Console
GOOGLE_CLIENT_SECRETS_PATH=./credentials.json
GOOGLE_TOKEN_PATH=./token.json
GOOGLE_CALENDAR_ID=primary

# Instacart
INSTACART_API_KEY=

# Browserbase — fallback path for Instacart (see §10)
BROWSERBASE_API_KEY=
BROWSERBASE_PROJECT_ID=
```

### Google OAuth scope

Request **one** scope that covers both reading free/busy and writing events, in a single
consent:

```
https://www.googleapis.com/auth/calendar
```

Requesting `calendar.events` alone will 403 on free/busy reads. Generate `token.json`
once with a local-server flow and commit nothing.

### `config.py`

```python
from zoneinfo import ZoneInfo

TIMEZONE            = ZoneInfo("America/Los_Angeles")
MODEL               = "claude-opus-5"

# Cooking windows
COOK_WINDOW_START   = "17:30"   # no 6am cooking
COOK_WINDOW_END     = "21:30"
MIN_SLOT_MINUTES    = 25        # shorter gaps aren't worth surfacing
SLOT_BUFFER_MINUTES = 15        # prep/cleanup pad added to est_time_minutes

# Planning
MEALS_PER_WEEK      = 5
W_EXPIRY            = 0.55      # weights must sum to 1.0
W_OVERLAP           = 0.25
W_COMPLETENESS      = 0.20
DIVERSITY_PENALTY   = 0.60      # multiplier when cuisine already used this week

# Delivery
DELIVERY_LEAD_HOURS = 4         # earliest realistic turnaround from ordering
DELIVERY_BUFFER_HRS = 2         # margin between delivery end and first cook slot

UNITS = {"g", "kg", "ml", "l", "cup", "tbsp", "tsp", "unit"}
CUISINES = {"italian", "mexican", "indian", "chinese", "japanese", "thai",
            "mediterranean", "american", "korean", "middle_eastern", "other"}
```

---

## 4. Database schema

`sql/schema.sql` — paste into the Supabase SQL editor and run once.

```sql
create extension if not exists pgcrypto;

-- 1. RECIPES ------------------------------------------------------------------
create table recipes (
  id                 uuid primary key default gen_random_uuid(),
  source_url         text,
  title              text not null,
  cuisine            text,
  est_time_minutes   int  not null,
  servings           int  default 2,
  steps              jsonb not null default '[]'::jsonb,
  raw_caption        text,
  extraction_status  text not null default 'pending',
  created_at         timestamptz default now()
);

create table recipe_ingredients (
  id               uuid primary key default gen_random_uuid(),
  recipe_id        uuid references recipes(id) on delete cascade,
  name             text not null,          -- normalized: lowercase, singular
  quantity         numeric,                -- NULLABLE: null for "a good glug"
  unit             text,
  qualitative_note text
);
create index on recipe_ingredients (recipe_id);
create index on recipe_ingredients (name);

-- 2. PANTRY -------------------------------------------------------------------
create table pantry (
  id              uuid primary key default gen_random_uuid(),
  ingredient_name text not null,           -- normalized, matches recipe_ingredients.name
  quantity        numeric,
  unit            text,
  expiry_date     date,
  updated_at      timestamptz default now()
);
create index on pantry (ingredient_name);

-- 3. COOK SLOTS (from Google Calendar free/busy) ------------------------------
create table cook_slots (
  id                uuid primary key default gen_random_uuid(),
  week_start_date   date not null,
  slot_start        timestamptz not null,
  slot_end          timestamptz not null,
  duration_minutes  int not null,
  suitability_score numeric not null default 0,
  assigned          boolean not null default false
);
create index on cook_slots (week_start_date, slot_start);

-- 4. MEAL PLAN ----------------------------------------------------------------
create table meal_plan (
  id                 uuid primary key default gen_random_uuid(),
  recipe_id          uuid references recipes(id),
  cook_slot_id       uuid references cook_slots(id),
  week_start_date    date not null,
  planned_date       date not null,
  planned_start_time timestamptz not null,
  planned_end_time   timestamptz not null,
  score              numeric,
  score_reason       text,
  calendar_event_id  text,
  status             text not null default 'planned'  -- planned|scheduled|failed
);
create index on meal_plan (week_start_date);
create unique index on meal_plan (week_start_date, recipe_id);

-- 5. SHOPPING LIST ------------------------------------------------------------
create table shopping_list (
  id                            uuid primary key default gen_random_uuid(),
  week_start_date               date not null,
  ingredient_name               text not null,
  quantity_needed               numeric,
  unit                          text,
  resolved_instacart_product_id text,
  resolution_status             text not null default 'pending'
     -- pending|resolved|fallback_search|unresolved
);
create index on shopping_list (week_start_date);
create unique index on shopping_list (week_start_date, ingredient_name, unit);

-- 6. INSTACART ORDERS ---------------------------------------------------------
create table instacart_orders (
  id                     uuid primary key default gen_random_uuid(),
  week_start_date        date not null unique,
  cart_url               text,
  item_count             int default 0,
  unresolved_item_count  int default 0,
  delivery_window_start  timestamptz,
  delivery_window_end    timestamptz,
  delivery_event_id      text,
  method                 text,   -- api|browserbase|search_links
  created_at             timestamptz default now()
);

-- 7. EVAL LOG -----------------------------------------------------------------
create table eval_log (
  id            uuid primary key default gen_random_uuid(),
  stage         text not null,
     -- ingestion|extraction|availability|planning|shopping_list|instacart|calendar
  input_ref     text,
  success       boolean not null,
  retry_count   int default 0,
  duration_ms   int,
  error_message text,
  created_at    timestamptz default now()
);
create index on eval_log (stage, created_at);
```

**`week_start_date` is always the Monday of the target week**, computed once:

```python
def week_start(d=None):
    d = d or datetime.now(TIMEZONE).date()
    return d - timedelta(days=d.weekday())
```

---

## 5. Shared libraries

### `lib/normalize.py`

The single most important shared function. If the pantry says `tomato` and a recipe says
`Tomatoes`, nothing matches and the shopping list is silently wrong. **Both paths import
this — do not write a second copy.**

```python
def normalize_ingredient(name: str, qty: float | None, unit: str | None
                        ) -> tuple[str, float | None, str | None]:
    """Return (normalized_name, quantity, normalized_unit).

    - lowercase, strip, collapse whitespace
    - drop parentheticals and leading descriptors:
      "2 large ripe Tomatoes (diced)" -> "tomato"
    - naive singularization: trailing 'ies'->'y', 'oes'->'o', 's'->'' (skip
      known-plural-form words: hummus, couscous, molasses, greens, oats)
    - unit aliases -> the config enum:
      tablespoon/tablespoons/tbs/T -> tbsp
      teaspoon/teaspoons/t         -> tsp
      gram/grams/gm                -> g
      kilogram/kilo/kgs            -> kg
      milliliter/millilitre/mls    -> ml
      liter/litre/ltr              -> l
      cups/c                       -> cup
      clove/cloves/piece/pieces/whole/large/medium/small/None -> unit
    - unknown unit -> "unit"
    """
```

Write ~15 unit assertions for this at the bottom of the file and run it directly. It is
the cheapest possible insurance.

### `lib/llm.py`

Structured outputs make the JSON schema-valid at the API level, so retries exist for
**API errors and semantic validation** (unit not in the enum, negative quantity, empty
steps) — not for JSON parse failures.

```python
import anthropic, time
from config import MODEL
from lib.evals import log_eval

client = anthropic.Anthropic()

def call_llm_structured(prompt, output_model, stage, input_ref,
                        validate=None, max_retries=3, system=None):
    """output_model: a pydantic BaseModel class.
    validate: optional fn(parsed) -> list[str] of semantic errors.
    Returns a validated output_model instance, or raises ExtractionFailure."""
    t0 = time.time()
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(max_retries):
        try:
            response = client.messages.parse(
                model=MODEL,
                max_tokens=16000,
                system=system,
                messages=messages,
                output_format=output_model,
            )
            parsed = response.parsed_output
            errors = validate(parsed) if validate else []
            if not errors:
                log_eval(stage, input_ref, True, attempt,
                         int((time.time() - t0) * 1000))
                return parsed
            messages += [
                {"role": "assistant", "content": parsed.model_dump_json()},
                {"role": "user",
                 "content": "Those values failed validation:\n- "
                            + "\n- ".join(errors)
                            + "\nReturn the corrected object."},
            ]
        except anthropic.APIStatusError as e:
            last_error = str(e)
            if e.status_code in (400, 404):
                break            # not retryable
            time.sleep(2 ** attempt)
    log_eval(stage, input_ref, False, max_retries,
             int((time.time() - t0) * 1000), error_message=locals().get("last_error"))
    raise ExtractionFailure(input_ref)
```

Notes for whoever implements this:
- `client.messages.parse(..., output_format=PydanticModel)` returns `.parsed_output` as a
  validated instance. This is the current API — **not** the deprecated `output_format`
  top-level string param, and not a manual `json.loads` of a text block.
- Do not pass assistant prefills; they return a 400 on `claude-opus-5`.
- Thinking is on by default on this model. Leave it alone.
- Catch a chain, not one broad class: `NotFoundError` → `RateLimitError` →
  `APIStatusError` → `APIConnectionError`. 400s are not retryable; 429s and 5xx are.

### `lib/external.py`

```python
def call_external_api(fn, *args, stage, input_ref, **kwargs):
    """Log and swallow. Returns (result, ok). Caller decides the fallback.
    Never raises — one failed item must not kill the batch."""
    t0 = time.time()
    try:
        result = fn(*args, **kwargs)
        log_eval(stage, input_ref, True, 0, int((time.time()-t0)*1000))
        return result, True
    except Exception as e:
        log_eval(stage, input_ref, False, 0, int((time.time()-t0)*1000),
                 error_message=str(e)[:500])
        return None, False
```

Used for every Instacart call and every Calendar read/write.

### `lib/evals.py`

```python
def log_eval(stage, input_ref, success, retry_count=0,
             duration_ms=None, error_message=None) -> None: ...

def eval_report() -> dict:
    """Per stage: attempts, success rate, avg retries, p50/p95 duration_ms,
    and the list of error messages. Printed by run_eval.py."""
```

---

## 6. Stage 1 — `extract.py`

**Input:** an Instagram reel URL or pasted caption text.
**Output:** one `recipes` row + N `recipe_ingredients` rows.
**Logs to:** `eval_log` stage `ingestion` and `extraction`.

### Ingestion — `get_reel_text(url_or_text) -> tuple[str, str | None]`

Returns `(text, source_url)`. Tiered, each tier falling back to the next:

1. **`yt-dlp` metadata.** `yt_dlp.YoutubeDL({"skip_download": True}).extract_info(url)` →
   the `description` field is the reel caption. Fast, no auth, no audio processing.
   Most recipe reels put the full ingredient list in the caption.
2. **Audio transcript.** If the caption is under ~120 chars, download the audio
   (`-f bestaudio`) and transcribe it. Only worth it if tier 1 is thin.
3. **Pasted text.** If the arg doesn't start with `http`, treat it as the caption
   directly. This is the eval path and the guaranteed demo path.

Log which tier fired to `eval_log` stage `ingestion` — "caption worked on 5/6 reels,
2 needed the transcript" is a real reliability-brief line.

### Extraction schema

```python
from pydantic import BaseModel, Field
from typing import Literal

class Ingredient(BaseModel):
    name: str = Field(description="lowercase, singular, no descriptors: 'tomato'")
    quantity: float | None = Field(description="null if the amount is qualitative")
    unit: Literal["g","kg","ml","l","cup","tbsp","tsp","unit"]
    qualitative_note: str | None = Field(
        description="only when quantity is null, e.g. 'a good glug', 'to taste'")

class ExtractedRecipe(BaseModel):
    title: str
    cuisine: Literal["italian","mexican","indian","chinese","japanese","thai",
                     "mediterranean","american","korean","middle_eastern","other"]
    est_time_minutes: int = Field(description="active cooking time, 5-180")
    servings: int
    steps: list[str] = Field(description="ordered, imperative, one action each")
    ingredients: list[Ingredient]
```

### Prompt

```
You are extracting a structured recipe from an Instagram Reel caption or transcript.

Rules:
- Ingredient names: lowercase, singular, no descriptors. "2 large ripe Tomatoes,
  diced" -> name "tomato". Put preparation in the steps, never in the name.
- If an amount is qualitative ("a good glug", "to taste", "a handful"), set
  quantity to null and record the phrase in qualitative_note. Never invent a
  number. A wrong number silently corrupts the shopping list; a null does not.
- est_time_minutes is active cooking time, excluding marinating or chilling.
- steps: one action per step, imperative, in order. Omit engagement filler
  ("follow for more", "save this"). If the caption has no steps, infer the
  minimal sequence the ingredients imply.
- Include every edible ingredient, including salt, oil, and water if called for.

CAPTION:
{caption}
```

### Semantic validator

```python
def validate_recipe(r: ExtractedRecipe) -> list[str]:
    errs = []
    if not (5 <= r.est_time_minutes <= 180):
        errs.append(f"est_time_minutes {r.est_time_minutes} outside 5-180")
    if not r.steps:
        errs.append("steps is empty")
    if not r.ingredients:
        errs.append("ingredients is empty")
    for i in r.ingredients:
        if i.quantity is not None and i.quantity <= 0:
            errs.append(f"{i.name}: quantity must be positive or null")
        if i.quantity is None and not i.qualitative_note:
            errs.append(f"{i.name}: null quantity needs a qualitative_note")
    return errs
```

### Write path

Run every extracted ingredient through `normalize_ingredient()` **before** insert — the
model is instructed to normalize, but the helper is the enforcement point. Set
`extraction_status` to `success` or `failed`. Always store `raw_caption`.

**Done when:** `python -m stages.extract <url_or_file>` inserts one recipe with
correctly normalized ingredients, and a deliberately mangled caption records a `failed`
row plus an `eval_log` entry instead of crashing.

---

## 7. Stage 2 — `availability.py`

**Input:** Google Calendar free/busy for the target week.
**Output:** rows in `cook_slots`.
**Logs to:** `eval_log` stage `availability`.

### Algorithm

1. **Query free/busy** for `week_start` 00:00 → `week_start + 7d` 00:00, in `TIMEZONE`:
   ```python
   service.freebusy().query(body={
       "timeMin": start.isoformat(), "timeMax": end.isoformat(),
       "timeZone": str(TIMEZONE),
       "items": [{"id": GOOGLE_CALENDAR_ID}],
   }).execute()
   ```
   Wrap in `call_external_api`. Returns busy intervals in UTC.
2. **Invert** busy intervals into free gaps, per day.
3. **Clip** each gap to `[COOK_WINDOW_START, COOK_WINDOW_END]` for that local date.
4. **Drop** gaps shorter than `MIN_SLOT_MINUTES`.
5. **Score** each surviving gap:

   ```
   score  = min(duration_minutes / 60, 1.5)      # longer is better, capped
   score += 0.30   if 17:30 <= local start < 20:00      # prime dinner hour
   score -= 0.40   if local start >= 20:30              # too late to start cooking
   score -= 0.50   if duration < 90 and the gap is bounded by busy
                   blocks on BOTH sides                 # can't cook between meetings
   score += 0.20   if the day is Sat or Sun             # more relaxed cooking
   ```
6. **Insert** into `cook_slots` with `assigned = false`.

### Failure fallback

If the free/busy call fails, write a default slot set — 19:00–20:15 every evening of the
week, `suitability_score = 0.1` — and log the failure. Availability is upstream of
everything, so it must never hard-block the pipeline. Degraded output beats no output,
and "here's how it behaves when Google is unreachable" belongs in the brief.

### Idempotency

Delete all `cook_slots` for that `week_start_date` before inserting.

**Done when:** running it against a seeded busy calendar produces slots that visibly
dodge the meetings, and pulling the network cable still produces the fallback slots plus a
logged failure.

---

## 8. Stage 3 — `plan.py`

**Input:** `recipes`, `recipe_ingredients`, `pantry`, `cook_slots`.
**Output:** up to `MEALS_PER_WEEK` rows in `meal_plan`.
**Logs to:** `eval_log` stage `planning`.

**Scoring and assignment are fully deterministic.** No LLM in the decision path — the
only model call is generating the human-readable `score_reason` after the decision is
made. This keeps the planner debuggable and keeps the eval numbers about extraction
quality rather than planner flakiness.

### Per-recipe score

```python
def expiry_urgency(recipe, pantry):
    """0..1 — how much this recipe rescues food about to go bad."""
    total = 0.0
    for ing in recipe.ingredients:
        p = pantry.get(ing.name)
        if not p or not p.expiry_date:
            continue
        days = (p.expiry_date - today).days
        if days <= 1:   total += 1.0
        elif days <= 7: total += (7 - days) / 6.0
    return min(1.0, total)

def pantry_overlap(recipe, pantry):
    """0..1 — fraction of ingredients already on hand."""
    return sum(1 for i in recipe.ingredients if i.name in pantry) / len(recipe.ingredients)

def completeness(recipe, pantry):
    """0..1 — penalizes recipes needing a big shop. 8+ missing items scores 0."""
    missing = sum(1 for i in recipe.ingredients if i.name not in pantry)
    return max(0.0, 1.0 - missing / 8.0)

base = (W_EXPIRY       * expiry_urgency(r, pantry)
      + W_OVERLAP      * pantry_overlap(r, pantry)
      + W_COMPLETENESS * completeness(r, pantry))
```

### Constrained assignment

```python
slots = cook_slots_for_week(sorted by slot_start)     # chronological
unassigned = all recipes with extraction_status == 'success'
used_cuisines = set()
planned = 0

for slot in slots:
    if planned >= MEALS_PER_WEEK:
        break
    capacity = slot.duration_minutes - SLOT_BUFFER_MINUTES
    candidates = [r for r in unassigned if r.est_time_minutes <= capacity]
    if not candidates:
        continue                                       # nothing fits, skip the slot
    def adjusted(r):
        s = base_score[r.id]
        if r.cuisine in used_cuisines:
            s *= DIVERSITY_PENALTY
        return s
    pick = max(candidates, key=adjusted)
    insert meal_plan(
        recipe_id          = pick.id,
        cook_slot_id       = slot.id,
        planned_start_time = slot.slot_start,
        planned_end_time   = slot.slot_start + timedelta(minutes=pick.est_time_minutes),
        planned_date       = slot.slot_start.astimezone(TIMEZONE).date(),
        week_start_date    = week_start,
        score              = adjusted(pick),
    )
    mark slot assigned; unassigned.remove(pick); used_cuisines.add(pick.cuisine); planned += 1
```

Iterating slots chronologically and always taking the highest-scoring fit means
soonest-expiring ingredients naturally land on the earliest evenings — which is exactly
the behaviour to narrate in the demo.

### `score_reason` (one LLM call per meal, non-blocking)

Prompt with the recipe title, the expiring pantry items it uses, the slot time, and the
recipe duration. Ask for **one sentence, under 20 words, no preamble**. Example target
output: *"Uses the spinach expiring Tuesday, and fits Monday's 50-minute gap."*

If the call fails, fall back to a template string. Never let this block the plan.

### Optional: the re-plan loop

`plan.py` takes `--exclude-slot <uuid>` (repeatable). Stage 5 uses it when delivery can't
arrive in time. See §10.

**Done when:** with hand-seeded pantry and slots, the plan is reproducible run to run, no
recipe is assigned to a slot it doesn't fit, and no cuisine repeats unless it has to.

---

## 9. Stage 4 — `shopping_list.py`

**Input:** `meal_plan` + `recipe_ingredients` + `pantry` for the week.
**Output:** rows in `shopping_list`, all `resolution_status = 'pending'`.
**Logs to:** `eval_log` stage `shopping_list`.

### Algorithm

1. Sum required quantities across all planned recipes, grouped by
   `(normalized_name, unit)`.
2. Subtract pantry stock **only when the unit matches exactly**.
3. **Unit-mismatch rule:** if the recipe wants `cup` and the pantry holds `g`, do not
   convert — buy the full amount. Cross-dimension conversion (volume ↔ mass) is
   ingredient-specific and unsolvable in general; a documented rule beats a silently
   wrong converter. State this as a known limitation in the brief.
4. **Qualitative rule:** a `null` quantity (oil, salt, "to taste") becomes
   `quantity_needed = 1, unit = 'unit'` if absent from the pantry, and is skipped
   entirely if present. Don't order 1g of salt.
5. Drop non-positive results. Insert what remains.

### Idempotency

Delete the week's `shopping_list` rows before inserting.

**Done when:** a pantry containing 400g of an item a recipe needs 300g of produces no
row, and 200g produces a row for exactly 100g.

---

## 10. Stage 5 — `instacart.py`

**Input:** `shopping_list` rows with `resolution_status = 'pending'`, plus
`MIN(meal_plan.planned_start_time)` for the week.
**Output:** one `instacart_orders` row with `cart_url` and a delivery window.
**Logs to:** `eval_log` stage `instacart`.

### Three tiers — try in order, record which one worked in `instacart_orders.method`

**Tier 1 — Instacart Developer Platform API (`method = 'api'`).**
The shopping-list-page endpoint takes plain ingredient names and returns a shoppable URL;
it needs no product-ID resolution. Post the line items, read the returned products link,
mark the rows `resolved`.

**Tier 2 — Browserbase (`method = 'browserbase'`).**
If the API key doesn't work or the endpoint isn't available, drive a real browser session:
open Instacart, search each item, add the first reasonable result to the cart, then
capture the cart URL. Rows that get added are `resolved`; rows the agent couldn't find are
`unresolved` with `unresolved_item_count` incremented. Keep a hard per-item timeout so one
stuck search can't eat the demo.

**Tier 3 — deterministic search links (`method = 'search_links'`).**
Always works, zero dependencies: `https://www.instacart.com/store/s?k={quote(name)}` per
item, rows marked `fallback_search`. This is the guaranteed floor — the pipeline always
produces something clickable, so the calendar stage always has a link to embed.

Every tier boundary goes through `call_external_api`, so each failure is logged rather
than raised, and the tier ladder shows up in the reliability brief as graceful degradation
rather than as an outage.

### Delivery window

```python
earliest_cook = min(planned_start_time for the week)
now           = datetime.now(TIMEZONE)

delivery_end   = earliest_cook - timedelta(hours=DELIVERY_BUFFER_HRS)
delivery_start = delivery_end  - timedelta(hours=2)

if delivery_start < now + timedelta(hours=DELIVERY_LEAD_HOURS):
    # Can't physically arrive before the first meal.
    # Minimum viable: re-run plan.py with --exclude-slot <that slot id>,
    # which pushes the meal to the next feasible window, then recompute.
    # Log the re-plan to eval_log with stage 'planning'.
```

That re-plan is the one genuine feedback edge in the system — the pipeline reconsidering
its own output because the physical world said no. It's the strongest available answer to
"is this an agent or a cron job?", and it is fifteen seconds of demo footage. Build it
only once stages 1–6 are green end to end.

If the chosen tier can't actually set a delivery window, keep the computed window as
advisory and still place it on the calendar — the user-facing behaviour is identical.

**Done when:** a pending shopping list yields a clickable URL and a delivery window that
ends at least `DELIVERY_BUFFER_HRS` before the first cook slot, and killing the API key
still produces a Tier 3 cart plus a logged failure.

---

## 11. Stage 6 — `calendar_sync.py`

**Input:** `meal_plan` joined to `recipes`, `recipe_ingredients`, and the week's
`instacart_orders`.
**Output:** `calendar_event_id` on each `meal_plan` row, `delivery_event_id` on the order.
**Logs to:** `eval_log` stage `calendar`.

### Meal event

```python
{
  "summary": f"🍳 {recipe.title}",
  "start": {"dateTime": planned_start_time.isoformat(), "timeZone": str(TIMEZONE)},
  "end":   {"dateTime": planned_end_time.isoformat(),   "timeZone": str(TIMEZONE)},
  "description": (
      f"{score_reason}\n\n"
      f"⏱ {est_time_minutes} min · {cuisine} · serves {servings}\n\n"
      f"INGREDIENTS\n{ingredient_lines}\n\n"
      f"STEPS\n{numbered_steps}\n\n"
      f"🛒 Groceries: {cart_url}\n"
      f"📹 Reel: {source_url}"
  ),
  "reminders": {"useDefault": False,
                "overrides": [{"method": "popup", "minutes": 30}]},
}
```

Ingredient lines render qualitative amounts honestly: `olive oil — a good glug`, not
`olive oil — 1 unit`.

### Delivery event

One event over `[delivery_window_start, delivery_window_end]`, titled
`🛒 Instacart delivery — {item_count} items`, description containing the cart URL and the
meals it unblocks. Store the returned id in `instacart_orders.delivery_event_id`.

### Hard requirements

- **Explicit `timeZone` on every insert.** Free/busy returns UTC; naive datetimes will put
  every meal at 2 AM and there is no time to debug that at 3 PM. Every datetime in this
  codebase is timezone-aware. No exceptions.
- **Idempotency:** skip any `meal_plan` row where `calendar_event_id IS NOT NULL`. Demos
  get re-run, and duplicate events on stage is the most embarrassing possible failure.
- Set `status = 'scheduled'` on success, `'failed'` on failure. One failed insert must not
  abort the rest — that's what `call_external_api` is for.

**Done when:** the events appear in the real calendar at the right local times, a second
run creates zero duplicates, and revoking the token leaves `status = 'failed'` rows with
logged errors rather than a traceback.

---

## 12. Orchestrator — `run_week.py`

```bash
python run_week.py                         # full pipeline for the coming week
python run_week.py --reels reels.txt       # ingest these reel URLs first
python run_week.py --from plan             # resume from a given stage
python run_week.py --dry-run               # everything except Calendar writes
```

Runs stages 1–6 in order and **prints its decisions as it goes.** The narration is the
demo — judges should be able to follow the reasoning without reading code:

```
[1/6] extract      6 reels → 6 recipes (2 needed the audio transcript)
[2/6] availability 11 open windows found, 4 viable for cooking
[3/6] plan         5 meals scheduled
      Mon 18:45  Palak Paneer          score 0.81  spinach expires Tue
      Tue 19:30  Chicken Tinga Tacos   score 0.64  chicken expires Thu
      Thu 18:00  Miso Salmon           score 0.52  fits the 40-min gap
      ...
[4/6] shopping     31 ingredients → 12 to buy (19 already in the pantry)
[5/6] instacart    cart ready (api) · delivery Mon 12:00–14:00
[6/6] calendar     6 events created (5 meals + 1 delivery)

Done. eval_log: 21 calls, 21 succeeded, 2 retries, p95 1,840ms
```

---

## 13. Evaluation — `run_eval.py`

This is a **graded deliverable**, worth 25% of the score — second only to technical
execution. It is not optional instrumentation.

```bash
python run_eval.py
```

1. Run all 5–8 captions in `seed/captions/` through `extract.py`.
2. Report per stage: attempts, success rate, average retries, p50/p95 latency,
   and every error message.
3. **Accuracy against ground truth**, not just schema validity. Structured outputs
   guarantee the JSON parses — that makes a "100% success rate" meaningless on its own.
   For the 3 hand-labelled captions in `tests/ground_truth/`, compute ingredient-set
   precision and recall against the labels. *Extraction parsed 100% and recovered the
   right ingredients 94% of the time* is a real claim; *100% schema valid* is a vanity
   metric, and a judge from Userlens will ask which one you measured.
4. Print a markdown table that pastes straight into the reliability brief.

### Reliability brief outline

- **Architecture** — six stages, database-as-contract, why availability precedes planning.
- **Failure handling** — the retry wrapper, the external-API wrapper, the availability
  fallback, the three Instacart tiers, per-item isolation.
- **Measured numbers** — straight from `eval_report()`.
- **Known limitations, stated plainly** — no cross-dimension unit conversion; qualitative
  amounts become 1 unit; free/busy can't tell "free" from "free but not at home";
  single-store assumption.

Naming the limitations yourself is worth more than hoping nobody asks.

---

## 14. Seed data

### `seed_pantry.py` — 15–20 items, varied expiry

This is the demo's *"why did it pick these meals"* evidence. Include 2–3 items expiring
within 48 hours (spinach, chicken thighs, cilantro), several mid-range, and staples with
no expiry (rice, flour, olive oil, soy sauce). Names must be normalized, and at least a
few must overlap with the test-set recipes — otherwise expiry urgency is always zero and
the planner has nothing to demonstrate.

### `seed_calendar.py` — a realistically busy week

**Ten minutes of work, and the headline feature is invisible without it.** On an empty
calendar every window is free, so "InstaCook found the evenings you're actually free"
demonstrates nothing. Seed: standups, a 6–7:30 PM gym block, one dinner out, a late
Thursday meeting ending at 8:30, and a packed Wednesday with no viable gap at all. That
last one matters — it proves the planner *skips* days it can't use.

---

## 15. Ownership split

Stages talk only through the database, so both paths proceed independently against
hand-inserted rows.

### Path A — Ingestion, Pantry, Eval
`sql/schema.sql` · `lib/normalize.py` · `lib/llm.py` · `lib/evals.py` ·
`stages/extract.py` · `stages/shopping_list.py` · `seed/seed_pantry.py` ·
`seed/captions/` · `tests/ground_truth/` · `run_eval.py`

Owns the shared helpers because extraction is their first consumer. Ships
`normalize_ingredient` **first** — Path B's planner needs it for pantry matching.

### Path B — Calendar, Planner, Instacart
Google OAuth · `lib/external.py` · `stages/availability.py` · `stages/plan.py` ·
`stages/instacart.py` · `stages/calendar_sync.py` · `seed/seed_calendar.py` ·
`run_week.py`

`shopping_list.py` sits with Path A because Path B picked up `availability.py` and needs
the offload; it's pure DB logic with no credentials, so it's the cleanest piece to move.

### Unblocking each other

- **Path B needs recipes before `extract.py` exists:** hand-insert 6 rows into `recipes` +
  `recipe_ingredients` with varied cuisines and `est_time_minutes` from 15 to 60. Fifteen
  of those minutes buys the whole afternoon.
- **Path A needs a plan before `plan.py` exists:** hand-insert 5 `meal_plan` rows.
- Agree on `sql/schema.sql` and `lib/normalize.py` **before** splitting. Everything
  downstream assumes them.

---

## 16. Timeline & cut list

Build closes **4:00 PM PT**; judging runs 4:00–4:40.

| Time | Path A | Path B |
|---|---|---|
| **→ 11:00** | Schema live, pantry seeded, `normalize.py` + `llm.py` + `evals.py` done and pushed | OAuth token (`auth/calendar` scope) verified with a real free/busy call, Instacart access confirmed, **calendar seeded**, mock recipe rows inserted |
| **11:00 – 12:30** | `extract.py` + 5–8 captions collected | `availability.py` → real `cook_slots` |
| **12:30 – 1:45** | `shopping_list.py` + 3 ground-truth labels | `plan.py` scoring + assignment |
| **1:45 – 2:30** | `run_eval.py` | `instacart.py` + `calendar_sync.py` |
| **2:30 – 3:15** | *Together:* end-to-end integration, timezone fixes, `run_week.py` narration | |
| **3:15 – 4:00** | **Code freeze.** Demo video + reliability brief | |

### Cut list, in the order things get dropped

1. Audio transcription (tier 2 ingestion) — captions carry most reels
2. The re-plan loop (§10)
3. Instacart Tier 1/2 — fall back to Tier 3 search links
4. `score_reason` LLM call — fall back to a template
5. `seed_calendar.py` variety — one busy day is enough to make the point

**Never cut:** `run_eval.py`, ground-truth labels, the delivery calendar event, or the
video. The 3:15 code freeze holds regardless of what's unfinished — a working system with
no video scores zero on two of five criteria.

---

## 17. Invariants

Rules every stage depends on. Breaking one causes a silent wrong answer rather than a
crash, which is why they're listed explicitly.

1. **Every datetime is timezone-aware.** Never construct a naive `datetime`. Always
   `timestamptz` in Postgres, always an explicit `timeZone` on Calendar inserts.
2. **Every ingredient name passes through `normalize_ingredient()` before it touches the
   database.** One helper, imported by both paths. No second copy.
3. **`quantity` is nullable and null means qualitative.** Never invent a number to satisfy
   a schema.
4. **Units are only compared, never converted across dimensions.** Mismatch means buy it.
5. **`week_start_date` is always the Monday of the target week.** Single helper.
6. **Every stage writes to `eval_log` on success and on failure.** No exceptions — this is
   the graded artifact.
7. **Every stage is safe to re-run.** Delete-then-insert for derived tables
   (`cook_slots`, `meal_plan`, `shopping_list`); skip-if-present for external side effects
   (`calendar_event_id`, `delivery_event_id`).
8. **No stage imports another stage.** Database rows are the only interface.
9. **External failures log and return `None`.** The caller picks a fallback; one bad item
   never kills the batch.
10. **Nothing named `calendar.py`.** It shadows the standard library.

---

## 18. Demo script

Two minutes, and the last twenty seconds are worth 25% of the score.

| Time | Beat |
|---|---|
| **0:00–0:15** | Six recipe reels I saved this week. My fridge — spinach expires in 2 days. My calendar — a genuinely busy week. |
| **0:15–0:35** | One command. It pulls the recipes off Instagram, then reads my calendar to find the evenings I'm actually free. |
| **0:35–1:10** | **Narrate the decisions slowly — this is the part that wins.** *Spinach dish on Monday, because it expires first. In the 7:15 window, because that 40-minute recipe doesn't fit Tuesday's 25-minute gap. Wednesday gets skipped entirely — there's no room.* |
| **1:10–1:30** | The real Instacart cart: only what I'm missing, with delivery timed to land before Monday's cook slot. |
| **1:30–1:45** | My actual Google Calendar. A meal each night at the right hour, the delivery window blocked out, ingredients and steps in every description. |
| **1:45–2:00** | The eval table: N reels, X% extraction success, Y% ingredient accuracy against hand-labelled ground truth, p95 latency, every stage logged — and here's a failure and how it recovered. |

If the re-plan loop shipped, trade 15 seconds of the cart beat for it: *"delivery couldn't
make Monday, so it moved that meal to Tuesday on its own."* That clip is worth more than
anything else in the video.

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in credentials
# paste sql/schema.sql into the Supabase SQL editor, run once
python seed/seed_pantry.py
python seed/seed_calendar.py
python run_week.py --reels reels.txt
```
