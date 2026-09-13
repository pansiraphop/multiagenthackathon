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
*(ElevenLabs voice guidance is scaffolded in the web app — see §6)*

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
                                           ▼        │ apply the answer
                              [4 shopping_list]     │ (reschedule / swap)
                                           │        │
                                           ▼        │
                               [5 instacart]        │
                                           │        │
                                           ▼        │
                              [5b followup] ────────┘
                                    │      ▲
                     asks ──────────┘      └────────── answers
                       │                                  │
              Instagram DM (write) ─────▶ you ─────▶ webhook.py
                                           │
                                           ▼
                    [6 calendar_sync] ──▶ Google Calendar (write)
                                           │
                                           ▼
                            [7 web app]  ← recipe page + pantry, §6
```

Six pipeline stages plus a web app the calendar links to. Each stage is a standalone script
with one job. **Stage 5b is the only cycle in the graph**, and it only closes through a
human: when the physical world makes the plan infeasible, the agent asks the person who
saved the reels and applies what they choose.

**Stages never import each other — they communicate only through Supabase rows.** That's
the whole reason two people can build this in parallel: either of us can hand-insert rows
to fake an upstream stage and get to work immediately.

### Why availability runs before planning

Reading the calendar is *upstream* of the planner, not downstream. The planner doesn't
just rank recipes, it fits them: a 45-minute recipe can't go in a 25-minute gap. That
makes cooking time a hard constraint rather than a display field, and it's the most
interesting thing the system does.

---

## 2. Ground rules

Short list. Each one prevents a silently wrong answer rather than a crash, which is why
they're worth agreeing on up front.

1. **One normalization helper, imported by both paths** — build every ingredient row with
   `normalize.to_ingredient_row()`. Lowercase, singular, units from a fixed enum. If the
   pantry says `tomato` and a recipe says `Tomatoes`, nothing matches and the shopping
   list is quietly wrong. This is the single most likely source of silent bugs in the
   whole project.
2. **Every datetime is timezone-aware.** `timestamptz` in Postgres, explicit `timeZone`
   on every Calendar write. Free/busy returns UTC; a naive datetime puts every meal at
   2 AM and there's no time to debug that at 3 PM.
3. **Every ingredient gets a usable amount, and estimates are labelled.** "A good glug of
   olive oil" has no exact number, but someone is standing at a stove reading this — a
   null is useless to them. So the model estimates for that specific ingredient (a
   handful of parsley is not a handful of almonds), sets `is_approximate`, and keeps the
   source's own words in `qualitative_note`. `render_amount()` shows it as
   `~2 tbsp (a good glug)`. The rule is not "never invent a number" — it's **never invent
   one silently.**
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
10. **The planner fits on `total_time_minutes`, never `est_time_minutes`.** A recipe has
    three different times and only one of them books an evening: active hands-on work,
    the **attended** span you must be home for (active plus braising, roasting, proving),
    and advance prep you need not be present for (marinating, chilling, rising). Fitting
    on active time schedules a two-hour braise into a forty-minute gap. Use
    `schemas.attended_minutes()`, which falls back to active time when total is missing —
    under-booking a slot is safe, over-booking a braise is not. `advance_prep_minutes`
    means the cook must start the day before, so it belongs in the calendar description.

Two shared wrappers, both in `lib/`: one for LLM calls (retry + validate + log) and one
for external API calls (try + log + return `None`). Write each once; don't end up with
two retry patterns.

---

## 3. Data model

Supabase / Postgres. Path A owns the DDL. This is the contract — the columns that other
stages depend on. Add whatever else you need.

| Table | Key columns | Written by |
|---|---|---|
| `recipes` | `id`, `source_url`, `sender_id`, `title`, `cuisine`, `est_time_minutes`, `total_time_minutes`, `advance_prep_minutes`, `servings`, `steps` (jsonb), `raw_caption`, `raw_transcript`, `extraction_status`, `provenance`, `source_sufficiency` | 1 |
| `recipe_ingredients` | `recipe_id`, `name`, `quantity`, `unit`, `is_approximate`, `qualitative_note` | 1 |
| `pantry` | `ingredient_name`, `quantity`, `unit`, `expiry_date` | seed |
| `cook_slots` | `week_start_date`, `slot_start`, `slot_end`, `duration_minutes`, `suitability_score`, `assigned` | 2 |
| `meal_plan` | `id`, `recipe_id`, `cook_slot_id`, `week_start_date`, `planned_start_time`, `planned_end_time`, `score`, `score_reason`, `calendar_event_id`, `status` | 3, 6 |
| `shopping_list` | `week_start_date`, `ingredient_name`, `quantity_needed`, `unit`, `resolution_status` | 4, 5 |
| `instacart_orders` | `week_start_date`, `cart_url`, `item_count`, `unresolved_item_count`, `delivery_window_start`, `delivery_window_end`, `delivery_event_id`, `method` | 5, 6 |
| `followups` | `week_start_date`, `kind`, `meal_plan_id`, `recipient_id`, `channel`, `question`, `options` (jsonb), `status`, `reply_text`, `chosen_key`, `resolution`, `round` | 5b |
| `eval_log` | `stage`, `input_ref`, `success`, `retry_count`, `duration_ms`, `error_message` | all |

`shopping_list.resolution_status` is `pending` → `added_to_cart` (in this week's cart, never
ordered again) / `failed` (browser couldn't add it) / `fallback_link` (search link only).
`instacart_orders.method` is `browser_automation` / `fallback_links` / `mixed`.

`recipes.sender_id` is the Instagram-scoped id of whoever DM'd the reel. Stage 5b messages
that person back, and the swap options it offers are that person's own earlier reels.

`followups.kind` is `delivery_conflict` / `missing_ingredients`; `status` is `sent` →
`answered` / `unreachable` (no DM channel, so the agent decided alone) / `superseded` (a
newer question replaced it). `options` is `[{key, action, label, recipe_id?, slot_id?}]`
where `action` is `later` / `swap` / `keep` — and it is deliberately the only thing needed
to act on a reply, since `meal_plan_id` may point at a row that stage 3 has since replaced.

`meal_plan.id` is the stable handle for a single meal. **The cook-session URL in §6 is
built from it**, so don't regenerate those rows once the calendar has been written.

---

## 4. Stages

### 1 · `extract.py` — reel → structured recipe
Reads a pending reel's caption (± transcript); writes structured fields onto the
same `recipes` row and its `recipe_ingredients`.

Instagram DMs enter through `webhook.py`. It acknowledges Meta immediately, then
`lib/ingest.py` parses every `ig_reel` attachment in a background task. The attachment's
`payload.title` is the primary caption. When `INGEST_TRANSCRIBE=true`, yt-dlp downloads
the audio and local Whisper produces a best-effort transcript; download/transcription
failure never discards a usable caption. Ingestion writes one deduplicated `recipes` row
per `source_url` with `extraction_status = 'pending'`.

**Transcript reliability is checked before extraction.** `lib/source.py` scores the
Whisper text with deterministic heuristics (length, recipe cue words, overlap with the
caption). Music-only reels that produce fluent junk are dropped (`tier=caption_only`).
A usable voiceover is labeled and concatenated after the caption
(`tier=caption_plus_transcript`); a thin caption with a strong transcript flips to
`transcript_primary`. The agent picks which pending reel to process, then runs:

```bash
python -m stages.extract --list          # pending rows + reliability scores
python -m stages.extract --best 1        # extract the top-ranked reel
python -m stages.extract --id <uuid>     # extract one recipe
python -m stages.extract --dry-run --best 1   # score + preview SOURCE, no LLM
```

Extraction consumes those rows' `raw_caption` + optional `raw_transcript`, then marks
each row `success` or `failed`. There is intentionally no users table or auth for the
single-user demo.

Ingestion is tiered: webhook caption first, audio transcription as a fallback for thin
captions, pasted text as the guaranteed path. Log which tier fired — *"captions worked
on 5/6 reels; transcript accepted on 2/6"* is a real brief line.


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
walk the slots chronologically and take the best-scoring recipe that actually fits —
`attended_minutes(recipe) + SLOT_BUFFER_MINUTES <= slot.duration_minutes` — penalizing
cuisines already used that week. A recipe that exceeds every window must be reported as
unschedulable, not silently dropped. Walking slots in time order means
soonest-expiring ingredients land on the earliest evenings for free.

**Two adjustments happen during the walk, not in the base score, because both depend on
what has already been picked.** Cuisines already used are discounted by
`DIVERSITY_PENALTY` (variety), and a recipe gets `W_SHARED_INGREDIENTS` × the fraction of
its ingredients another chosen meal already needs (cost — one bunch of coriander across
two dinners beats two half-used bunches). The shared bonus is deliberately smaller than
`W_EXPIRY`, so a cheaper shop never outranks rescuing food that expires tomorrow.

One LLM call per meal generates `score_reason` — a one-sentence human-readable "why this
meal, why now" for the calendar description. Falls back to a template if it fails.

Should accept an "exclude this slot" flag, for the re-plan case in stage 5.

### 4 · `shopping_list.py` — what's actually missing
Reads `meal_plan`, `recipe_ingredients`, `pantry`; writes `shopping_list`.

Sums requirements across the week grouped by **(name, dimension)**, so `2 cup` and
`100 ml` of the same thing merge into one purchase while `3 tbsp butter` and `250 g
butter` stay separate — volume-to-mass depends on the ingredient, so it's never inferred
(ground rule 4). Pantry stock is subtracted on the same rule: it counts when it's the
same dimension, and a mismatch means buy the full amount.

Two details that matter for how the cart reads. It renders in the recipes' own unit when
they agreed — `1 tsp turmeric`, not `4.93 ml` — falling back to the base unit only when
sources disagree, and scaling up large amounts (`1.53 l`, not `1530 ml`). And it never
lists an ingredient as both bought and pantry-covered, which happens legitimately when one
recipe wants it by weight and another by count but reads as a contradiction.

`is_non_food()` rows are filtered out: water, pasta water and ice are real recipe
ingredients but must never reach a cart.

**Re-running mid-week does not re-buy the week's groceries.** The table is
delete-then-insert, so a reel arriving on Wednesday rebuilds the list — and without care
every row would read `pending` again and stage 5 would order everything twice.
`carry_over_statuses()` keeps `added_to_cart` for any row the current cart already covers,
and only when the new requirement is *not larger* than what was already carted. If two
more meals now need spinach, that row goes back to `pending` so the shortfall still gets
bought.

### 5 · `instacart.py` — cart + delivery window
Reads `shopping_list`; writes `instacart_orders`.

The Instacart Developer Platform is not currently accepting new applications, so stage 5
uses **two tiers, recorded in `method`**: Browserbase drives a real, persistent Instacart
session and adds each ingredient to the cart; plain `instacart.com` search links are the
zero-dependency floor. The floor matters: stage 6 always needs *some* URL to embed.

Browser automation is isolated per item. Every selector wait has a timeout, a transient
failure is retried once, and a final failure saves a screenshot under `failures/`, logs
the ingredient, and continues. Sessions explicitly allow 30 minutes so a 28-item batch
doesn't hit Browserbase's five-minute project default; if a session still expires,
already-added items remain successful instead of being relabeled as fallback failures.
A browser-session or expired-login failure falls back to links for the whole list instead
of dead-ending the pipeline. Run
`python scripts/instacart_probe.py tomato` once to log in through Browserbase's live view,
confirm the live selectors, and persist the resulting `BROWSERBASE_CONTEXT_ID`.

**One cart per week — the guard is the expensive thing to get wrong.** Reels arriving all
week accumulate into a single order, so re-running stage 5 must never add the same
groceries again. The decision comes from the existing `instacart_orders` row plus each
`shopping_list.resolution_status`:

| Existing state | What a re-run does |
|---|---|
| Cart built, nothing pending | Skips entirely, logs it, touches no browser |
| Cart built, some rows pending | Resumes — adds *only* the missing rows |
| `fallback_links` only | Retries the full browser cart; nothing was ever added, so nothing can duplicate |
| `--force` | Re-adds everything, and says out loud that it may duplicate |

Two more cost details. The same ingredient can appear twice with different units (`250 g
butter`, `3 tbsp butter`) — that is one product in a cart, so it is searched once and both
rows are marked. And a row with a blank ingredient name is refused rather than searched,
because an empty query adds whatever Instacart happens to show first.

`method` therefore has a third value: **`mixed`**, when a resumed top-up fails and the week
ends up with a real cart plus a few link-only items. `item_count` is counted from the
table, not from the run, so a resumed order reports the whole week's cart rather than just
the items it topped up.

`--dry-run` prints the whole decision — dedupe, guard state, and every URL it would open —
without a browser session, a database write, or even an `eval_log` row.

Pick a delivery window that ends comfortably before the earliest cook slot. If nothing
feasible exists, that is not stage 5's decision to make on its own — see stage 5b.

### 5b · `followup.py` — ask, don't guess
Reads `instacart_orders` + `meal_plan` + `shopping_list` + `recipes`; writes `followups`,
sends an Instagram DM, and re-runs stages 3, 4 and 6 when an answer arrives.

Two things can invalidate a finished plan, and neither is a bug: the groceries can't land
before the earliest cook slot, or the cart couldn't resolve what a meal needs. Both could
be resolved silently — drop the slot, re-plan around it — and that is a reasonable
*fallback* but a poor *first move*, because only the user knows whether they'd rather move
Monday's dinner, cook something else that night, or pick up two things themselves.

So the agent asks, in the same Instagram thread the reels arrived in, and every option is
concrete:

```
Heads up - the groceries don't land until Mon 20:30, which is 220 minutes
too late for Weeknight Palak Paneer at Mon 18:50.

Reply with a number:
1. Move Weeknight Palak Paneer later this week
2. Cook Rigatoni alla Vodka with Cherry Tomatoes that night instead
3. Keep it and I'll shop for the rest myself
```

**Option 2 is the interesting one: it's a reel the user sent earlier and hasn't been
planned this week.** Candidates are filtered on the same hard constraints the planner uses
(it has to fit that window, and its advance prep has to be startable in time) and ranked so
that a dish needing an ingredient the cart already failed on sorts last.

**Answers come back through `webhook.py`,** which already receives that thread — a text
message is routed to stage 5b instead of stage 1. Replies are matched on the quick-reply
payload, then a single number anywhere in the message ("yes, 1"), then an exact label, then
one unambiguous keyword. Anything that names two options, or none, gets a short "reply with
just the number" rather than a guessed action: **acting on a misread reply would move a real
evening in someone's real calendar.**

Applying an answer is what makes this a loop rather than a notification. `later` re-plans
the week without that window and rebuilds the shopping list; `swap` edits just that one
`meal_plan` row, keeping the window the user deliberately chose, and rebuilds the list;
`keep` changes nothing. Then the calendar is re-synced and **the new plan gets exactly the
same feasibility check as the old one** — which can produce the next question.

Three properties keep that safe:

- **It terminates.** `FOLLOWUP_MAX_ROUNDS` bounds question → answer → re-plan. Past the
  bound the agent stops asking, resolves the week deterministically, and says so.
- **It doesn't nag.** An unanswered question for the same `kind` blocks a second one, so
  re-running the pipeline never re-sends it. Asking a *new* question supersedes the old,
  because answering a question about a plan that no longer exists is worse than silence.
- **It degrades.** With no `INSTAGRAM_ACCESS_TOKEN` the question is printed and recorded as
  `unreachable`, and the old deterministic re-plan runs immediately — the floor is the
  behaviour this stage replaced, not a dead end.

`python -m stages.followup --dry-run` decides and prints the DM without writing anything;
`--reply "2"` acts as if that answer arrived; `--status` shows the week's conversation.

### 6 · `calendar_sync.py` — write the week
Reads `meal_plan` + `recipes` + `instacart_orders`; writes Google Calendar, stores event
ids back.

One event per meal at its real cook time. Description carries `score_reason`, ingredients
rendered with `render_amount()` so estimates read as `olive oil — ~2 tbsp (a good glug)`,
numbered steps, the cart URL, the reel link, and **the cook-session URL from §6**. If the
recipe was reconstructed, say so in the description. Plus one event for the
delivery window. Explicit `timeZone`, skip rows that already have an event id.

---

## 5. Supporting scripts

- **`run_week.py`** — runs the pipeline in order and **prints its decisions as it goes.**
  The narration is the demo; judges should follow the reasoning without reading code.
  `--dry-run` is read-only. **Stage 5 is opt-in behind `--instacart`** — building a cart
  is the one step with real-world consequences, so testing can never place an order.
  It also owns the feedback edge: if `plan.delivery_conflict()` finds the groceries can't
  reach the earliest cook slot, it re-plans without that window and rebuilds the list.
- **`run_eval.py`** — see §7.
- **`seed_pantry.py`** — 29 items: some expiring within 48 hours, some in weeks, staples
  with no expiry, and one already expired to prove expired stock is ignored rather than
  treated as urgent. Names go through the normalizer on the way in; if the pantry says
  `Tomatoes` and a recipe says `tomato`, nothing matches and the whole thing quietly
  fails.
- **`seed_calendar.py`** — a realistically busy week. **Ten minutes, and the headline
  feature is invisible without it:** on an empty calendar every window is free, so
  "InstaCook found the evenings you're actually free" demonstrates nothing. Include one
  day with no viable gap at all — proving the planner *skips* days is harder than proving
  it picks good ones.

---

## 6. Stage 7 — the web app

`web/` is the site the calendar invite links to. Three pages, server-rendered:

| Route | What it is |
|---|---|
| `/` | This week's meals, and the single collated shopping list |
| `/cook/{meal_plan_id}` | **What the calendar links to.** A recipe page you can cook from |
| `/pantry` | What's in the fridge, grouped by how soon it goes off |

```bash
uvicorn web.app:app --reload --port 8000
```

### Why it's server-rendered Python and not a JS app

**RLS is disabled on every table.** A browser-side app has to ship the Supabase key, and
with RLS off that key grants full read *and write* on the whole database — in a demo
video. Server-rendering keeps it on the server.

It also avoids writing the data layer twice. `render_amount()`, `attended_minutes()`,
`advance_prep()` and `is_non_food()` are Python, tested, and have each had real bugs fixed
in them. Reimplementing those rules in TypeScript is how a `~2 tbsp (a good glug)` quietly
becomes `2 tbsp`.

No template engine, no build step, no new dependencies — FastAPI was already here for the
webhook. `web/views.py` is plain functions returning strings, so the pages are testable
without a browser.

### Designed for a phone at a stove

Someone opens this with one hand, oily fingers, mid-step. So: 48px touch targets, nothing
that depends on hover, tap-anywhere ingredient and step checkoff persisted per device,
a sticky action bar inside the iOS safe-area inset, and the **Wake Lock API** so the
screen doesn't die between steps. Estimates are marked `est.` — a guess must never read as
a measurement.

Warm paper ground, one clay accent, serif dish names against system-sans UI, hairline
rules, full dark mode. Restraint is the point.

### The voice half — ready, not wired

The guided-cooking button is on the page and honestly disabled. `window.InstaCook.context`
already carries the recipe, steps and ingredients the agent needs, so switching it on is:
read that JSON, hand it to a **hosted ElevenLabs conversational agent** as session context,
drop the `disabled` attribute. Their agent ships as a plain web component, so no framework
is needed.

Do not hand-roll an STT→LLM→TTS loop. That's the difference between an afternoon and a day.

### The pantry, and where it's going

The page reads real pantry rows and its add/remove forms work. But the intended path is
the **Instagram agent**: you tell it what you bought and it fills the pantry in for you.
The manual form is the fallback and the way to watch the data model work before that lands.

### One thing that will bite

`APP_BASE_URL` is baked into each calendar description **at write time**. Point it at
wherever the app is actually reachable (`ngrok http 8000` is enough) *before* running stage
6, or the link in the invite will say `localhost` on your phone. Changing it afterwards
means `calendar_sync --clear` and re-running.

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
isolation, the bounded follow-up loop) · the measured numbers · and **known limitations
stated plainly** — no
cross-dimension unit conversion, qualitative amounts approximated, free/busy can't tell
"free" from "free but not at home", and the cart takes the first search result rather than
comparing brands or sizes, so it optimizes *what* to buy rather than which product to buy.
Naming the limits ourselves beats hoping nobody asks.

**Instacart reliability note:** Browserbase was chosen because the formal Instacart API
path was unavailable during the hackathon window. Browser action-taking has more
per-step failure risk than REST, so stage 5 explicitly retries transient selector
failures, captures a screenshot on final item failure, reuses one authenticated session
for the batch, and falls back to plain search links. Report browser-automation and
fallback-link success separately using `instacart_orders.method`.

**Follow-up loop note:** stage 5b is the strongest answer to *"is this an agent or a cron
job?"*, so it's worth being precise about its limits. Meta only allows a business to send a
DM inside 24 hours of the user's last message, so a question raised days after the last reel
arrived will be rejected by the Send API — which lands in the `unreachable` path and
deterministic re-plan, not in a crash. Replies are matched conservatively and an ambiguous
answer is re-asked rather than guessed. And the loop is bounded: `followups.round` and
`FOLLOWUP_MAX_ROUNDS` mean the worst case is three questions, not an infinite exchange.
Quote `followups` directly in the brief — every row is one time the physical world said no,
what was offered, what was chosen, and what the agent then did.

---

## 8. Ownership

Stages talk only through the database, so both paths proceed independently.

### Already built — import these, don't rewrite them

The schema is **live in Supabase**; `sql/schema.sql` and `prisma/schema.prisma` are the
source of truth (see `DATABASE.md`). The shared layer is done and tested:

| Module | What's in it |
|---|---|
| `config.py` | Every tunable, credentials, `TIMEZONE`, `week_start()`, `now_local()` |
| `lib/normalize.py` | `to_ingredient_row()` (the write path), `parse_quantity()`, `normalize_measure()`, `render_amount()`, `dedupe_ingredients()`, `is_non_food()`, `clean_source_text()`. ~80 self-tests |
| `lib/db.py` | Supabase client, `insert_recipe()`, `insert_ingredients()`, `successful_recipes()`, `delete_where()` |
| `lib/llm.py` | `call_llm_structured()` (retry + semantic validation) and `call_llm_with_search()` |
| `lib/schemas.py` | `ExtractedRecipe`, `Ingredient`, `DishIdentification`, `validate_recipe()`, `needs_reconstruction()` |
| `lib/prompts.py` | Extraction, dish identification, reconstruction, and `score_reason` prompts |
| `lib/evals.py` | `log_eval()`, `timed()` context manager, `eval_report()`, `format_report()` |
| `lib/external.py` | `call_external_api()` — returns `(result, ok)`, never raises |
| `lib/google_auth.py` | OAuth with one read+write scope; re-consents if a cached token is too narrow |
| `web/views.py` | Page rendering as pure functions — testable without a browser |
| `lib/ingest.py` | Reel payload parsing, caption-first local Whisper fallback, deduplicated pending-recipe write |
| `lib/instagram.py` | Outbound DMs with quick replies, console fallback tier, reply parsing, `match_option()` |
| `lib/source.py` | Transcript reliability score + caption/transcript SOURCE assembly for extraction |
| `lib/browser.py` | Browserbase session, overlay dismissal, failure screenshots, `with_retry()` |
| `webhook.py` | FastAPI Meta verification + background Reel ingestion **and follow-up replies** on port 8000 |
| `stages/extract.py` | Agent entrypoint: list/rank pending reels, extract caption±transcript into recipes |

Stages built: **1** `stages/extract.py`, **2** `availability.py`, **3** `plan.py`,
**4** `shopping_list.py`, **5** `instacart.py`, **5b** `followup.py`,
**6** `calendar_sync.py`, **7** `web/`.
`run_week.py` runs them in order. Seeds:
`seed_pantry.py`, `seed_calendar.py`.

Run `python -m lib.normalize`, `python -m lib.schemas`, and `python -m lib.source` for
self-tests.


### Tests

```bash
python -m unittest discover -s tests -t .   # all unit suites, no network, <1s
python -m lib.normalize                     # normalizer self-tests
python -m lib.schemas                       # schema + validator self-tests
python -m lib.source                        # transcript reliability self-tests
python -m stages.extract --list             # pending reels + reliability (needs Supabase)
python -m stages.extract --best 1           # LIVE extraction of top pending reel
python -m tests.test_pipeline               # LIVE integration, stages 1-4
python -m tests.test_pipeline --keep        # ...and leave the rows in place
python -m tests.test_pipeline --fixtures    # recorded recipes, no model spend
```

The unit suites are stdlib `unittest`, deterministic, and hit nothing external — a
failure means logic changed, not that an API was slow. `test_pipeline` is deliberately
excluded from discovery because it spends real API calls and writes real rows.

What `test_pipeline` proves is the **handoff**, not the stages. Each stage passing alone
doesn't mean the planner can do anything: extraction has to produce recipes whose
**attended** time fits inside the windows availability found, and the narrow windows have
to actually exclude something — otherwise the fit constraint isn't doing any work and the
demo has no story. It asserts both, and it cleans up after itself.

**Build every ingredient row with `to_ingredient_row()`** — it is the only thing that
guarantees all of the invariants at once. Verified against real reel captions, it handles
unicode and mixed fractions (`1½`, `½`), ranges (`2-3` → 2.5, flagged approximate),
imperial conversion (`8 oz` → 227 g, `1 stick` → 113 g, `1½ lbs` → 680 g), measure words
stuck in names (`3 cloves garlic` → `garlic`), qualitative estimates, and duplicate lines.
`dedupe_ingredients()` then merges repeats, and `is_non_food()` keeps pasta water and ice
out of the shopping list while leaving them in the recipe.

Imperial conversion matters more than it looks: without it `8 oz` normalizes to
`8 unit` — the number survives but means something entirely different, which is worse
than failing outright.

`week_start()` returns the **upcoming** Monday (or today, if today is Monday) — the week
being planned. Not the Monday of the current week: running on a Sunday would otherwise
schedule everything six days in the past.

**Path A — ingestion, pantry, eval.** `stages/extract.py` · `shopping_list.py` · pantry seed ·
test captions and ground-truth labels · `run_eval.py`.

**Path B — calendar, planner, instacart.** Google OAuth · the external-API wrapper ·
`availability.py` · `plan.py` · `instacart.py` · `calendar_sync.py` · calendar seed ·
`run_week.py`.

**Stage 7:** `voiceover.py` is built and touches nothing else. The conversational cook
session is still open — whoever is free first.

The schema and the normalizer are **already settled and built** (see above) — everything
downstream assumes them, so don't reimplement either. If you need a contract changed,
change it here first and say so.

To unblock each other: hand-insert ~6 `recipes` rows with varied cuisines and
`est_time_minutes` from 15 to 60 so Path B can build the planner before extraction exists;
hand-insert `meal_plan` rows so Path A can build the shopping list before the planner
exists. Fifteen minutes of fake rows buys the whole afternoon.

### Credentials, first thing

Google OAuth: request the single `https://www.googleapis.com/auth/calendar` scope so one
consent covers both free/busy reads and event writes — `calendar.events` alone will 403 on
reads. Verify a real free/busy call returns before writing planner code. For Instacart,
fill `BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID`, run the one-time interactive
probe, then save its context id as `BROWSERBASE_CONTEXT_ID`; future runs reuse that
logged-in browser context.

For stage 5b's outbound DMs, generate an Instagram access token for the professional
account the reels are sent to (Meta app → Instagram → *API setup with Instagram login*,
scope `instagram_business_manage_messages`) and put it in `INSTAGRAM_ACCESS_TOKEN`. It is
optional: without it the agent prints its question and re-plans on its own, which is the
same code path as an expired token.

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

**Cut in this order:** audio transcription → the stage 7 conversational agent → the stage 5b
DM loop (it degrades to the deterministic re-plan by removing one env var) → Instacart
browser automation (fall back to search links) → the `score_reason` LLM call.

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

**The re-plan loop shipped, so show it — it is the single beat that proves this is an agent
and not a cron job.** Delivery can't make Monday, so the agent DMs you on Instagram with
three options, you tap *"cook the vodka rigatoni that night instead"*, and the calendar
rewrites itself while the video is still playing. Force it on camera by pushing
`delivery_window_end` past the first cook slot and re-running stage 5b.

The stage 7 voiceover gives the demo two possible closings. Narrate the 0:35–1:10 beat
with the generated MP3 instead of a live voice — the system explaining its own reasoning
is a stronger version of the same thirty seconds. Or end on the cook-along: tap the
calendar link and hear it read the first step back to you.

---

## Setup

```bash
brew install ffmpeg      # macOS; required only for local Whisper transcription
brew install python@3.11 # local Whisper/PyTorch does not yet support Python 3.14
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env     # fill Supabase, META_VERIFY_TOKEN, NGROK_URL, and other credentials
# fill Browserbase key/project, then log in once and save the printed context id:
python scripts/instacart_probe.py tomato
# apply the schema in the Supabase SQL editor
python seed/seed_pantry.py && python seed/seed_calendar.py
uvicorn webhook:app --host 0.0.0.0 --port 8000
# in another terminal: ngrok http 8000
# set NGROK_URL / WEBHOOK_URL in .env to the printed https URL, and use
# {WEBHOOK_URL} as the Meta callback (verify token = META_VERIFY_TOKEN).
# Subscribe to the `messages` field: it delivers both reels and follow-up replies.
python run_week.py --reels reels.txt
```

For a caption-only demo, set `INGEST_TRANSCRIBE=false`; webhook ingestion remains
functional. Local Whisper defaults to the `base` model via `WHISPER_MODEL`.

To exercise the follow-up loop without the DM channel:

```bash
python -m stages.followup --dry-run       # what it would ask, and the options
python -m stages.followup --reply "2"     # act as if you answered 2
python -m stages.followup --status        # the week's conversation so far
```
