# InstaCook

**Turn saved Instagram recipe reels into a cooked week.**

InstaCook reads a recipe reel, checks what's in your fridge and what's about to expire,
looks at your real Google Calendar to find the evenings you're actually free, and then
schedules the week: groceries ordered through Instacart timed to arrive before you cook,
and a calendar event for every meal with the ingredients, the steps, and the cart link
already in it.

Built for the **Multi-App AI Agent Hackathon** (Sunday, September 13, 2026).
Build closes 4:00 PM PT.

**Apps it acts across:** Instagram · Google Calendar (read + write) · Instacart
*(+ ElevenLabs, if the stretch goal lands — see §6)*

This README is the shared spec. It sets direction and fixes the contracts between stages;
implementation details get filled in as we build. If you change a contract, change it
here first and tell the other person.

---

## 1. Pipeline

```
Instagram reel ──▶ [1 extract] ──▶ recipes + ingredients
                                          │
Google Calendar ──▶ [2 availability] ──▶ cook_slots
       (read)                              │
                                           ▼
                                    [3 plan] ──▶ meal_plan
                                           │        ▲
                                           ▼        │ re-plan if delivery
                              [4 shopping_list]     │ can't arrive in time
                                           │        │
                                           ▼        │
                               [5 instacart] ───────┘
                                           │
                                           ▼
                    [6 calendar_sync] ──▶ Google Calendar (write)
                                           │
                                           ▼
                            [7 cook session]  ← optional stretch, §6
```

Six stages plus an optional seventh. Each stage is a standalone script with one job.

**Stages never import each other — they communicate only through Supabase rows.** That's
the whole reason two people can build this in parallel: either of us can hand-insert rows
to fake an upstream stage and get to work immediately.

### Why availability runs before planning

Reading the calendar is *upstream* of the planner, not downstream. The planner doesn't
just rank recipes, it fits them: a 45-minute recipe can't go in a 25-minute gap. That
makes `est_time_minutes` a hard constraint rather than a display field, and it's the most
interesting thing the system does.

---

## 2. Ground rules

Short list. Each one prevents a silently wrong answer rather than a crash, which is why
they're worth agreeing on up front.

1. **One `normalize_ingredient()` helper, imported by both paths.** Lowercase, singular,
   units from a fixed enum. If the pantry says `tomato` and a recipe says `Tomatoes`,
   nothing matches and the shopping list is quietly wrong. This is the single most likely
   source of silent bugs in the whole project.
2. **Every datetime is timezone-aware.** `timestamptz` in Postgres, explicit `timeZone`
   on every Calendar write. Free/busy returns UTC; a naive datetime puts every meal at
   2 AM and there's no time to debug that at 3 PM.
3. **Ingredient quantity is nullable, and null means qualitative.** "A good glug of olive
   oil" has no correct number. Never invent one to satisfy a schema.
4. **Units are compared, never converted across dimensions.** Cups↔grams is
   ingredient-specific and unsolvable in general. On a mismatch, buy the full amount and
   say so in the brief.
5. **Every stage writes to `eval_log` on success and failure** — with retry count and
   duration. This is a graded deliverable, not instrumentation.
6. **Every stage is re-runnable.** Delete-then-insert for derived tables; skip-if-present
   for anything with an external side effect. Demos get re-run, and duplicate calendar
   events on stage is the worst possible look.
7. **External calls log and return `None`.** The caller picks a fallback; one bad item
   never kills the batch.
8. **`week_start_date` is always the Monday of the target week.** One helper, used
   everywhere.
9. **Nothing named `calendar.py`** — it shadows the stdlib module and breaks
   `google-api-python-client` imports. Use `calendar_sync.py`.

Two shared wrappers, both in `lib/`: one for LLM calls (retry + validate + log) and one
for external API calls (try + log + return `None`). Write each once; don't end up with
two retry patterns.

---

## 3. Data model

Supabase / Postgres. Path A owns the DDL. This is the contract — the columns that other
stages depend on. Add whatever else you need.

| Table | Key columns | Written by |
|---|---|---|
| `recipes` | `id`, `source_url`, `title`, `cuisine`, `est_time_minutes`, `servings`, `steps` (jsonb), `raw_caption`, `extraction_status`, `provenance`, `source_sufficiency` | 1 |
| `recipe_ingredients` | `recipe_id`, `name`, `quantity` *(nullable)*, `unit`, `qualitative_note` | 1 |
| `pantry` | `ingredient_name`, `quantity`, `unit`, `expiry_date` | seed |
| `cook_slots` | `week_start_date`, `slot_start`, `slot_end`, `duration_minutes`, `suitability_score`, `assigned` | 2 |
| `meal_plan` | `id`, `recipe_id`, `cook_slot_id`, `week_start_date`, `planned_start_time`, `planned_end_time`, `score`, `score_reason`, `calendar_event_id`, `status` | 3, 6 |
| `shopping_list` | `week_start_date`, `ingredient_name`, `quantity_needed`, `unit`, `resolution_status` | 4, 5 |
| `instacart_orders` | `week_start_date`, `cart_url`, `item_count`, `delivery_window_start`, `delivery_window_end`, `delivery_event_id`, `method` | 5, 6 |
| `eval_log` | `stage`, `input_ref`, `success`, `retry_count`, `duration_ms`, `error_message` | all |

`meal_plan.id` is the stable handle for a single meal. **The cook-session URL in §6 is
built from it**, so don't regenerate those rows once the calendar has been written.

---

## 4. Stages

### 1 · `extract.py` — reel → structured recipe
Reads a reel URL or pasted caption; writes `recipes` + `recipe_ingredients`.

Ingestion is tiered: `yt-dlp` metadata gives the caption without auth (most recipe reels
put the full ingredient list there), audio transcription as a fallback for thin captions,
pasted text as the guaranteed path. Log which tier fired — *"captions worked on 5/6 reels"*
is a real brief line.

Extraction uses `client.messages.parse()` with a Pydantic model on `claude-opus-5`, which
makes the JSON schema-valid at the API level. **So the retry loop is for semantic
validation, not parsing** — unit outside the enum, negative quantity, empty steps. It also
means "100% schema valid" is a vanity metric; see §7.

#### Thin reels: reconstruct rather than fail

Plenty of food reels are beauty shots with music and no actual recipe. Those must not
hard-fail. **Reconstruction produces a synthetic caption, which then goes back through the
same extractor** — one schema, one validator, one set of eval plumbing:

```
transcript ──▶ extract ──▶ recipe
                  │
                  └─ insufficient? ──▶ research (web search) ──▶ synthetic recipe text
                                                                        │
                                                                        └─▶ extract ──▶ recipe
```

- **Escalate on a deterministic rule, not a model opinion.** Pass 1 returns
  `source_sufficiency` (`complete` / `partial` / `insufficient`); Python escalates when
  that isn't `complete`, or ingredients < 3, or steps is empty. Keeping the branch in code
  makes it reproducible and countable — *"4 of 6 reels had usable transcripts, 2 were
  reconstructed"* only exists if the gate is deterministic.
- **Use Anthropic's server-side web search** for the research call —
  `{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}` on
  `claude-opus-5`. No extra search API. Two gotchas: don't also declare
  `code_execution` (that variant runs it internally, and a second execution environment
  confuses the model), and server-tool errors come back as HTTP 200 with an error object
  where a result list is expected — branch on it or you get a `TypeError` instead of a
  clean fallback. Keep the research output free-form prose; the extractor handles
  structure.
- **Never reconstruct from nothing.** Reconstruction needs a dish identity from the
  caption, hashtags, or on-screen title. With no usable text signal at all, set
  `extraction_status = 'failed'` and log it. A plausible recipe with no relationship to
  the reel is worse than a failure, and a reel of a curry producing a pasta dish is
  exactly what a judge will catch.
- **Label it.** `recipes.provenance` is `transcript` or `reconstructed`, and the calendar
  description says so plainly (*"Reconstructed — this reel didn't include a recipe"*).
  Visible honesty about it is a demo asset: it shows the system knows what it doesn't know.
- **It splits the eval into two populations.** Ground-truth precision/recall only applies
  to transcript-sufficient reels — there's nothing to compare a reconstruction against.
  Score those on completeness instead (ingredients ≥ 3, steps ≥ 3, time in range) and
  report the two groups separately, or both numbers become meaningless.

### 2 · `availability.py` — calendar → cook windows
Reads Google Calendar free/busy for the week; writes `cook_slots`.

Invert busy blocks into gaps, clip to a configured evening window, drop anything too
short, score what's left. Rough scoring intent: longer is better, prime dinner hours beat
late starts, and a short gap wedged between two meetings gets penalized — you can't cook
between calls. Tune the weights by looking at the output.

**Needs a fallback**, because this is now upstream of everything: if free/busy fails,
write default evening slots and log it. A degraded plan beats no plan, and "here's how it
behaves when Google is down" belongs in the brief.

### 3 · `plan.py` — pick meals, fit them to windows
Reads `recipes`, `pantry`, `cook_slots`; writes `meal_plan`.

**Deterministic scoring and assignment — no LLM in the decision path.** Score each recipe
on expiry urgency (weighted highest), pantry overlap, and how big a shop it implies. Then
walk the slots chronologically and take the best-scoring recipe that actually fits,
penalizing cuisines already used that week. Walking slots in time order means
soonest-expiring ingredients land on the earliest evenings for free.

One LLM call per meal generates `score_reason` — a one-sentence human-readable "why this
meal, why now" for the calendar description. Falls back to a template if it fails.

Should accept an "exclude this slot" flag, for the re-plan case in stage 5.

### 4 · `shopping_list.py` — what's actually missing
Reads `meal_plan`, `recipe_ingredients`, `pantry`; writes `shopping_list`.

Sum requirements across the week, group by normalized name + unit, subtract pantry stock
only when units match exactly (ground rule 4). Qualitative-quantity items become one
generic unit if absent and are skipped if present — don't order 1g of salt.

### 5 · `instacart.py` — cart + delivery window
Reads `shopping_list`; writes `instacart_orders`.

**Tiered, and record which tier fired in `method`:** the Developer Platform shopping-list
endpoint (takes plain ingredient names, returns a shoppable URL — no product-ID
resolution needed), then Browserbase driving a real session if the API isn't usable, then
plain `instacart.com` search links as a zero-dependency floor. The floor matters: stage 6
always needs *some* URL to embed.

Pick a delivery window that ends comfortably before the earliest cook slot. If nothing
feasible exists, re-run `plan.py` excluding that slot so the meal moves later, and log the
re-plan. **That feedback edge is the strongest answer to "is this an agent or a cron
job?"** — build it once 1–6 are green.

### 6 · `calendar_sync.py` — write the week
Reads `meal_plan` + `recipes` + `instacart_orders`; writes Google Calendar, stores event
ids back.

One event per meal at its real cook time. Description carries `score_reason`, ingredients
(rendering qualitative amounts honestly — `olive oil — a good glug`), numbered steps, the
cart URL, the reel link, and **the cook-session URL from §6**. Plus one event for the
delivery window. Explicit `timeZone`, skip rows that already have an event id.

---

## 5. Supporting scripts

- **`run_week.py`** — runs 1→6 in order and **prints its decisions as it goes.** The
  narration is the demo; judges should follow the reasoning without reading code.
  Flags worth having: resume-from-stage, and a dry-run that skips calendar writes.
- **`run_eval.py`** — see §7.
- **`seed_pantry.py`** — 15–20 items, some expiring in 48 hours, some in weeks, staples
  with no expiry. Must overlap with the test recipes or expiry urgency is always zero and
  the planner has nothing to show.
- **`seed_calendar.py`** — a realistically busy week. **Ten minutes, and the headline
  feature is invisible without it:** on an empty calendar every window is free, so
  "InstaCook found the evenings you're actually free" demonstrates nothing. Include one
  day with no viable gap at all — proving the planner *skips* days is harder than proving
  it picks good ones.

---

## 6. Stage 7 (optional) — voice-guided cook session

**ElevenLabs voice agent that walks you through cooking, hands-free.** The calendar invite
carries a link; you open it when you start cooking and an agent is already waiting, knows
which recipe you're making, and talks you through it — you can interrupt, ask to repeat a
step, ask what to substitute.

This is a genuine stretch goal. **It only gets built if stages 1–6 are green end to end**,
and it is the first thing cut. But three cheap decisions now mean it can be bolted on later
with zero rework:

### Decide now, build later

1. **The link ships in the calendar description from the very first version**, pointing at
   `{APP_BASE_URL}/cook/{meal_plan.id}`. Stage 6 then never has to change. If stage 7
   never happens, that URL serves a plain recipe page — ingredients, steps, cart link.
   Still useful, still demoable, never a dead link. This is the one thing that must be
   true before stage 6 is considered done.
2. **`meal_plan.id` is the session key.** Everything the agent needs — recipe, steps,
   ingredients, timings — is reachable from that one id, so the page needs no state of its
   own and no new tables.
3. **Use ElevenLabs' hosted conversational agent, not a hand-rolled STT→LLM→TTS loop.**
   Drop their embed on a static page and pass the recipe in as session context. That's the
   difference between an afternoon and a day. Building our own voice pipeline is out of
   scope today, full stop.

### Rough shape

A single static page: read the meal from Supabase by id, render the recipe, mount the
ElevenLabs agent with the recipe and steps injected as context, and prompt it to act as a
cooking guide — one step at a time, wait for the user, answer questions. Deploy anywhere
that gives a public URL in one command.

If it lands, ElevenLabs becomes a fourth external app and the demo gets a genuinely
memorable closing beat. If it doesn't, nothing else in the system is affected.

---

## 7. Evaluation & the reliability brief

Reliability and evaluation is **25% of the score — second only to technical execution**,
and it's the one thing that can't be retrofitted at 3:30 PM.

`run_eval.py` runs a fixed set of 5–8 real reel captions through extraction and reports,
per stage: attempts, success rate, average retries, p50/p95 latency, and every error
message. Output should paste straight into the brief as a markdown table.

**Measure accuracy, not just validity.** Structured outputs guarantee the JSON parses, so
a 100% success rate says nothing on its own. Hand-label the ingredient lists for 3 of the
captions and report precision/recall against them. *"Parsed 100%, recovered the right
ingredients 94% of the time"* is a real claim; *"100% schema valid"* is a vanity metric,
and a judge will ask which one we measured.

The brief covers: architecture and why availability precedes planning · failure handling
(retry wrapper, external wrapper, availability fallback, Instacart tiers, per-item
isolation) · the measured numbers · and **known limitations stated plainly** — no
cross-dimension unit conversion, qualitative amounts approximated, free/busy can't tell
"free" from "free but not at home". Naming the limits ourselves beats hoping nobody asks.

---

## 8. Ownership

Stages talk only through the database, so both paths proceed independently.

**Path A — ingestion, pantry, eval.** Schema/DDL · `normalize_ingredient` · the LLM
wrapper · eval logging · `extract.py` · `shopping_list.py` · pantry seed · test captions
and ground-truth labels · `run_eval.py`.

**Path B — calendar, planner, instacart.** Google OAuth · the external-API wrapper ·
`availability.py` · `plan.py` · `instacart.py` · `calendar_sync.py` · calendar seed ·
`run_week.py`.

**Stage 7, if it happens:** whoever is free first. It touches nothing else.

Agree on the schema and `normalize_ingredient()` **before splitting** — everything
downstream assumes them, and Path B's planner needs the normalizer for pantry matching.

To unblock each other: hand-insert ~6 `recipes` rows with varied cuisines and
`est_time_minutes` from 15 to 60 so Path B can build the planner before extraction exists;
hand-insert `meal_plan` rows so Path A can build the shopping list before the planner
exists. Fifteen minutes of fake rows buys the whole afternoon.

### Credentials, first thing

Google OAuth: request the single `https://www.googleapis.com/auth/calendar` scope so one
consent covers both free/busy reads and event writes — `calendar.events` alone will 403 on
reads. Verify a real free/busy call returns before writing planner code. Confirm what
Instacart access actually exists early, since it decides which tier of stage 5 we build.

---

## 9. Timeline & cut list

| Time (PT) | Path A | Path B |
|---|---|---|
| **→ 11:00** | Schema live, pantry seeded, shared helpers pushed | OAuth verified with a real free/busy call, Instacart access known, **calendar seeded**, mock recipe rows in |
| **11:00 – 12:30** | `extract.py`, captions collected | `availability.py` |
| **12:30 – 1:45** | `shopping_list.py`, ground-truth labels | `plan.py` |
| **1:45 – 2:30** | `run_eval.py` | `instacart.py`, `calendar_sync.py` |
| **2:30 – 3:15** | *Together:* end-to-end integration, timezone fixes, `run_week.py` narration | |
| **3:15 – 4:00** | **Code freeze.** Demo video + reliability brief | |

Stage 7 has no slot in this timeline on purpose. It happens only if we're genuinely early.

**Cut in this order:** audio transcription → stage 7 voice agent → the re-plan loop →
Instacart tiers 1 and 2 (fall back to search links) → the `score_reason` LLM call.

**Never cut:** `run_eval.py`, the ground-truth labels, or the video. The 3:15 freeze holds
regardless of what's unfinished — a working system with no video scores zero on two of
five criteria.

---

## 10. Demo (2 minutes)

| Time | Beat |
|---|---|
| 0:00–0:15 | Six reels I saved. My fridge — spinach expires in 2 days. My calendar — a genuinely busy week. |
| 0:15–0:35 | One command. It pulls the recipes off Instagram, then reads my calendar for the evenings I'm actually free. |
| 0:35–1:10 | **Narrate the decisions slowly — this is the part that wins.** *Spinach dish Monday, because it expires first. The 7:15 window, because that 40-minute recipe doesn't fit Tuesday's 25-minute gap. Wednesday skipped entirely — no room.* |
| 1:10–1:30 | The real Instacart cart: only what's missing, delivery timed to land before Monday's cook slot. |
| 1:30–1:45 | My actual Google Calendar — a meal each night at the right hour, delivery window blocked out, ingredients and steps in every description. |
| 1:45–2:00 | The eval table: extraction success, ingredient accuracy against hand labels, p95 latency, every stage logged — plus one failure and how it recovered. |

The last 15 seconds carry 25% of the score. Don't trade them for more product footage.

If the re-plan loop shipped, show it: *"delivery couldn't make Monday, so it moved that
meal to Tuesday on its own."* If stage 7 shipped, end on it — tap the calendar link, the
voice agent greets you by recipe name.

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env     # Supabase, Anthropic, Google OAuth, Instacart, Browserbase
# apply the schema in the Supabase SQL editor
python seed/seed_pantry.py && python seed/seed_calendar.py
python run_week.py --reels reels.txt
```
