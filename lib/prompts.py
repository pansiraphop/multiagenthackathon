"""Prompts for stage 1. Kept apart from code so they can be edited without
touching logic, and so the reliability brief can quote them verbatim.
"""

EXTRACTION_SYSTEM = (
    "You extract structured recipes from Instagram Reel captions and transcripts. "
    "You are precise about what the source actually says and never pad a recipe "
    "with plausible-sounding detail the source does not support."
)

EXTRACTION_PROMPT = """\
Extract a structured recipe from this Instagram Reel caption or transcript.

## Ingredient names
- Lowercase, singular, no descriptors or amounts. "2 large ripe Tomatoes, diced"
  -> name "tomato". Preparation belongs in the steps, never in the name.
- Split combined lines into separate ingredients. "salt and pepper to taste"
  becomes two entries. "olive oil or butter" becomes one entry for the first
  option.
- Translate non-English ingredient names to English, keeping widely-used terms
  as they are ("gochujang", "tahini", "miso").
- Never list the same ingredient twice. If it appears in two places (a marinade
  and the pan), combine into a single entry with the total amount.

## Amounts — always give a usable number
Every ingredient needs an amount a cook can act on. Someone is standing at a
stove reading this; "olive oil: unspecified" is useless to them.

- When the source states an amount, use it exactly and leave is_approximate
  false.
- When the source is qualitative, **estimate a sensible number for that
  specific ingredient**, set is_approximate true, and put the source's own
  words in qualitative_note. The cook sees "~2 tbsp (a good glug)", so your
  estimate is useful without being mistaken for a measurement.
- Use the ingredient to judge the estimate. A handful of parsley is not a
  handful of almonds. These are starting points, not rules:

  | Source says | Typical estimate |
  |---|---|
  | a glug / drizzle / splash of oil | 1 tbsp |
  | a good or generous glug | 2 tbsp |
  | a knob of butter | 1 tbsp |
  | a pinch | 1/4 tsp |
  | a dash | 1/4 tsp |
  | salt/pepper "to taste" | 1/2 tsp |
  | a handful of herbs | 1/4 cup |
  | a handful of nuts | 40 g |
  | a squeeze of lemon | 1 tbsp |
  | a few sprigs | 2 unit |
  | a bunch of herbs | 1 unit |

- Convert imperial and volumetric units to the allowed set: ounces and pounds
  to g, fluid ounces and pints to ml, a stick of butter to 113 g.
- For a range ("2-3 cloves"), use the midpoint and set is_approximate true.
- Only use null if there is genuinely nothing to estimate from.

## Steps
- One action per step, imperative, in order.
- Drop engagement filler: "follow for more", "save this", "link in bio",
  "comment RECIPE".
- If the source lists no method, infer the minimal sequence the ingredients
  imply.

## Time — three different numbers, and getting them wrong breaks scheduling
These feed a calendar. The cook's evening is booked from `total_time_minutes`,
so treat that as the important one.

- **est_time_minutes** — ACTIVE hands-on work only. Chopping, searing,
  stirring, plating. No waiting of any kind.
- **total_time_minutes** — the whole span the cook must be AT HOME, from
  starting to sitting down to eat. Active work PLUS unattended cooking they
  still have to be around for: braising, roasting, baking, simmering,
  reducing, proving in a warm oven, resting a roast. For a quick stir-fry this
  equals est_time_minutes. For a braise it is far larger. Never smaller than
  est_time_minutes.
- **advance_prep_minutes** — lead time BEFORE that session during which the
  cook need not be present at all: marinating, brining, chilling, setting,
  soaking dried beans, an overnight rise, freezing. 0 if none.

The test is simple: *could they leave the house?* If no, it belongs in
total_time_minutes. If yes, it belongs in advance_prep_minutes.

| Recipe | est | total | advance |
|---|---|---|---|
| 10-minute garlic butter noodles | 10 | 10 | 0 |
| Palak paneer, 35 min start to finish | 35 | 35 | 0 |
| Short rib ragu: sear 30 min, braise 2 h | 30 | 150 | 0 |
| Roast chicken: 20 min prep, 90 min oven, 15 min rest | 20 | 125 | 0 |
| Fried chicken marinated overnight, fried 20 min | 20 | 30 | 720 |
| No-bake cheesecake: 20 min assembly, chill 4 h | 20 | 20 | 240 |
| Focaccia: 20 min work, 8 h rise, 25 min bake | 25 | 45 | 480 |

A two-hour braise recorded as 30 minutes gets scheduled into a 40-minute gap
and the plan is undeliverable. When a step says "braise for two hours", that
time is attended.

## Other fields
- If the source contains more than one recipe, extract only the main one — the
  dish the reel is actually about.
- Include every edible ingredient, salt, oil and water included when called for.
- source_sufficiency is your honest read on the SOURCE, not on your output:
  - "complete"     — the source specified both ingredients and method
  - "partial"      — the source named some ingredients but left real gaps
  - "insufficient" — the source barely described the dish; you would be writing
                     the recipe from your own knowledge
  Judge the source. Do not mark it complete because you can fill the gaps
  yourself. Estimating a glug does not make a source incomplete — that is
  normal recipe language. Inventing half the ingredient list does.

SOURCE:
{source_text}
"""

IDENTIFY_DISH_PROMPT = """\
This Instagram Reel has little or no recipe information — it may be mostly food
photography with music.

Decide what dish it shows, using only the signal available: the caption, the
hashtags, any spoken words, any on-screen title.

Be specific where the source supports it ("gochujang butter noodles", not
"noodles"). Set confident to true ONLY if the source genuinely identifies the
dish. If all you have is an unlabelled shot of attractive food, set dish_name to
null and confident to false — a plausible recipe with no relationship to the
reel is worse than no recipe at all.

SOURCE:
{source_text}
"""

RECONSTRUCT_PROMPT = """\
Write a reliable, well-tested recipe for: {dish_name}

Search the web for two or three reputable versions of this dish and synthesize
them into one recipe that a home cook can follow and trust. Prefer the points
the sources agree on over any single source's flourishes.

Here is everything the original Reel provided. Honor any specific detail it does
give — a named ingredient, a technique, a garnish — and fill in only what is
missing:

ORIGINAL REEL SOURCE:
{source_text}

Write plain prose in this shape, and nothing else:

Title: <dish name>
Cuisine: <one word>
Time: <active cooking minutes>
Serves: <number>

Ingredients:
- <amount> <unit> <ingredient>
(one per line; write "to taste" where an amount genuinely varies)

Steps:
1. <one action>
2. <one action>

Give real quantities for anything measurable. Do not add commentary, sourcing
notes, or alternatives — the output is parsed by another program.
"""

VOICEOVER_SYSTEM = (
    "You write narration that will be spoken aloud by a text-to-speech voice, "
    "never read on a page. You write only what the brief supports: the brief is "
    "a record of decisions a system actually made, and a detail you add is a "
    "claim about someone's real calendar that is not true."
)

WEEK_VOICEOVER_PROMPT = """\
Write the spoken voiceover for a short demo of InstaCook, a system that turns
saved recipe reels into a cooked week. It read the user's real calendar, found
the evenings they were free, fitted recipes into those gaps, and ordered the
groceries to land before the first one.

The brief below is everything the system decided. Narrate those decisions.

## What makes this good
- **Lead with the reasoning, not the feature.** "Spinach on Monday, because it
  expires first" is the whole point. "InstaCook uses advanced scheduling" is
  filler.
- Name each meal and the evening it landed on, and say why when the brief
  gives a reason.
- If the brief says a day was skipped or a recipe didn't fit, say so plainly.
  A system that admits what it couldn't do sounds more trustworthy than one
  that doesn't, and it is the most memorable line available to you.
- Close on the delivery landing before the first cook, if the brief has it.

## How it must be written
- Around {target_words} words, and under {max_words}. It is read aloud at
  conversational pace over a two-minute demo — going long is the one failure
  that cannot be fixed in the edit.
- Plain spoken English. No markdown, no headings, no bullets, no emoji, no
  bracketed stage directions, no "e.g.".
- Write numbers and times the way they are said: "seven fifteen on Tuesday",
  "forty minutes", not "7:15pm" or "40 min".
- Short sentences. A voice model runs out of breath in a long one, and a
  listener runs out of attention first.
- Break it into segments of one idea each, so it can be re-cut without
  re-recording the whole thing. Labels are for the script file only and are
  never spoken.
- Never invent a dish, a day, an ingredient or a number that is not below.

BRIEF:
{brief}
"""

COOK_VOICEOVER_PROMPT = """\
Write a spoken cook-along for one meal. The cook opens this from their calendar
invite when they start cooking, and listens with their hands full — they cannot
look at a screen and they cannot scroll back.

## How it must be written
- Open by naming the dish and how long it takes. One sentence.
- Read the ingredients out as a short list, each with its amount, in the order
  they are used. Say approximate amounts as approximate: "about two
  tablespoons of olive oil".
- Then walk the steps, one at a time, in order, exactly as given. Do not merge
  two steps into one sentence and do not add a step of your own.
- Between steps, say what the cook should be looking for, but only if the step
  itself implies it. Do not invent techniques, temperatures or timings.
- Where a step has a wait in it, say so clearly so they know they have a gap.
- Close with one short line: the dish is done, and to enjoy it.
- Plain spoken English, under {max_words} words. No markdown, no bullets, no
  numbers written as digits where a word is natural, no emoji.
- One segment per step, plus the opening and the close, so playback can be
  paused between them.

MEAL:
{brief}
"""

SCORE_REASON_PROMPT = """\
For each planned meal below, write one sentence explaining why it was scheduled
then — for the cook to read in their calendar invite.

Under 20 words. No preamble, no "This meal was chosen because". Lead with the
concrete reason: an ingredient about to expire, or how the cooking time fits the
window available.

Good: "Uses the spinach expiring Tuesday, and fits Monday's 50-minute gap."
Bad:  "This meal was selected due to its high score and ingredient overlap."

Return one reason per meal, in the same order.

MEALS:
{meals}
"""
