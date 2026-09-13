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

## Other fields
- est_time_minutes is ACTIVE cooking time. Exclude marinating, chilling,
  resting and rising.
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
