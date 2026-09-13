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

Rules:
- Ingredient names: lowercase, singular, no descriptors or amounts. "2 large ripe
  Tomatoes, diced" -> name "tomato". Preparation belongs in the steps, never in
  the name.
- If an amount is qualitative ("a good glug", "to taste", "a handful"), set
  quantity to null and record the source's phrasing in qualitative_note. Never
  invent a number. A wrong number silently corrupts the shopping list; a null
  does not.
- est_time_minutes is active cooking time. Exclude marinating, chilling and
  resting.
- steps: one action per step, imperative, in order. Drop engagement filler
  ("follow for more", "save this", "link in bio"). If the source lists no
  method, infer the minimal sequence the ingredients imply.
- Include every edible ingredient, salt, oil and water included when called for.
- source_sufficiency is your honest read on the SOURCE, not on your output:
  - "complete"     — the source specified both ingredients and method
  - "partial"      — the source named some ingredients but left real gaps
  - "insufficient" — the source barely described the dish; you would be writing
                     the recipe from your own knowledge
  Judge the source. Do not mark it complete because you are able to fill in the
  gaps yourself.

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
