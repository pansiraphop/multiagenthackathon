"""Stage 1 — pending reel (caption ± transcript) -> structured recipe.

The agent picks which pending row to process; this script builds the SOURCE
text from caption + optional Whisper transcript (after a reliability check),
runs structured extraction, and writes the result back onto the same row.

    python -m stages.extract --list              # pending rows + reliability
    python -m stages.extract --best 1            # extract the top-ranked reel
    python -m stages.extract --id <uuid>         # extract one recipe by id
    python -m stages.extract --url <reel-url>    # extract by source_url
    python -m stages.extract --all               # every pending row
"""

from __future__ import annotations

import argparse
import sys

from lib import db
from lib.llm import LLMFailure, call_llm_structured, call_llm_with_search
from lib.normalize import dedupe_ingredients, to_ingredient_row
from lib.prompts import (
    EXTRACTION_PROMPT,
    EXTRACTION_SYSTEM,
    IDENTIFY_DISH_PROMPT,
    RECONSTRUCT_PROMPT,
)
from lib.schemas import (
    DishIdentification,
    ExtractedRecipe,
    needs_reconstruction,
    validate_recipe,
)
from lib.source import SourceDecision, decide_source, rank_pending

STAGE = "extraction"


def _print_decision(row: dict, decision: SourceDecision, priority: float | None = None) -> None:
    title = (row.get("raw_caption") or "")[:48].replace("\n", " ")
    prefix = f"{priority:0.2f}  " if priority is not None else ""
    print(
        f"{prefix}{row['id'][:8]}  tier={decision.tier:24s}  "
        f"cap={decision.caption_score:0.2f}  tr={decision.transcript_score:0.2f}  "
        f"chars={decision.caption_chars}/{decision.transcript_chars}"
    )
    print(f"         url={row.get('source_url')}")
    print(f"         preview={title!r}")
    print(f"         why={'; '.join(decision.reasons)}")


def list_pending() -> int:
    rows = db.pending_recipes()
    if not rows:
        print("No pending recipes.")
        return 0
    ranked = rank_pending(rows)
    print(f"{len(ranked)} pending recipe(s), ranked for extraction:\n")
    for priority, decision, row in ranked:
        _print_decision(row, decision, priority)
        print()
    return 0


def _resolve_targets(args: argparse.Namespace) -> list[dict]:
    if args.id:
        row = db.get_recipe(args.id)
        if not row:
            raise SystemExit(f"No recipe with id {args.id}")
        return [row]
    if args.url:
        rows = db.select("recipes", "*", source_url=args.url)
        if not rows:
            raise SystemExit(f"No recipe with source_url={args.url}")
        return rows
    pending = db.pending_recipes()
    if args.all:
        return pending
    if args.best:
        ranked = rank_pending(pending)
        return [row for _, _, row in ranked[: args.best]]
    raise SystemExit("Pick one of --list, --best N, --id, --url, or --all")


def _extract_from_source(
    source_text: str, input_ref: str
) -> tuple[ExtractedRecipe, bool, str]:
    """Return (recipe, was_reconstructed, source_sufficiency_of_the_ORIGINAL).

    Both extra values matter and neither can be recovered from the recipe
    afterwards. A successful reconstruction produces a complete-looking recipe,
    so asking needs_reconstruction() about the FINAL result answers "no" and
    silently labels a web-rebuilt recipe as coming from the reel. And the
    rebuilt pass reports its own sufficiency as 'complete', which would erase
    the honest assessment of the original source that the eval split needs.
    """
    parsed = call_llm_structured(
        EXTRACTION_PROMPT.format(source_text=source_text),
        ExtractedRecipe,
        stage=STAGE,
        input_ref=input_ref,
        validate=validate_recipe,
        system=EXTRACTION_SYSTEM,
    )
    source_sufficiency = parsed.source_sufficiency
    if not needs_reconstruction(parsed):
        return parsed, False, source_sufficiency

    print("      source thin — trying reconstruction")
    identity = call_llm_structured(
        IDENTIFY_DISH_PROMPT.format(source_text=source_text),
        DishIdentification,
        stage=STAGE,
        input_ref=f"identify:{input_ref}",
        system=EXTRACTION_SYSTEM,
    )
    if not identity.confident or not identity.dish_name:
        return parsed, False, source_sufficiency

    prose = call_llm_with_search(
        RECONSTRUCT_PROMPT.format(
            dish_name=identity.dish_name,
            source_text=source_text,
        ),
        stage=STAGE,
        input_ref=f"reconstruct:{input_ref}",
    )
    if not prose:
        return parsed, False, source_sufficiency

    rebuilt = call_llm_structured(
        EXTRACTION_PROMPT.format(source_text=prose),
        ExtractedRecipe,
        stage=STAGE,
        input_ref=f"reextract:{input_ref}",
        validate=validate_recipe,
        system=EXTRACTION_SYSTEM,
    )
    return rebuilt, True, source_sufficiency


def extract_recipe(row: dict, *, dry_run: bool = False) -> dict | None:
    """Score sources, extract, and update the same recipes row in place."""
    decision = decide_source(row.get("raw_caption"), row.get("raw_transcript"))
    _print_decision(row, decision)
    if dry_run:
        print("      dry-run: source preview")
        print(decision.source_text[:500])
        print("      …" if len(decision.source_text) > 500 else "")
        return None

    input_ref = row.get("source_url") or row["id"]
    try:
        parsed, reconstructed, source_sufficiency = _extract_from_source(
            decision.source_text, input_ref)
    except LLMFailure as exc:
        db.update("recipes", row["id"], {"extraction_status": "failed"})
        print(f"      FAILED: {exc}")
        return None

    # Based on whether the reconstruction path actually RAN, not on how
    # complete the end result looks.
    provenance = "reconstructed" if reconstructed else "transcript"
    # Keep original caption/transcript; overwrite structured fields.
    db.update("recipes", row["id"], {
        "title": parsed.title,
        "cuisine": parsed.cuisine,
        "est_time_minutes": parsed.est_time_minutes,
        "total_time_minutes": parsed.total_time_minutes,
        "advance_prep_minutes": parsed.advance_prep_minutes,
        "servings": parsed.servings,
        "steps": parsed.steps,
        "extraction_status": "success",
        "provenance": provenance,
        "source_sufficiency": source_sufficiency,
    })
    db.delete_where("recipe_ingredients", recipe_id=row["id"])
    rows = dedupe_ingredients([
        to_ingredient_row(
            i.name, i.quantity, i.unit, i.qualitative_note, i.is_approximate,
        )
        for i in parsed.ingredients
    ])
    db.insert_ingredients(row["id"], rows)

    print(
        f"      OK  {parsed.title!r}  active={parsed.est_time_minutes}  "
        f"attended={parsed.total_time_minutes}  "
        f"{len(rows)} ingredients  provenance={provenance}  "
        f"tier={decision.tier}"
    )
    return db.get_recipe_with_ingredients(row["id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true",
                        help="Show pending reels with transcript reliability")
    parser.add_argument("--best", type=int, metavar="N",
                        help="Extract the top N ranked pending reels")
    parser.add_argument("--id", help="Extract one recipe by UUID")
    parser.add_argument("--url", help="Extract by Instagram source_url")
    parser.add_argument("--all", action="store_true",
                        help="Extract every pending recipe")
    parser.add_argument("--dry-run", action="store_true",
                        help="Score and preview SOURCE text; no LLM / no writes")
    args = parser.parse_args(argv)

    if args.list:
        return list_pending()

    targets = _resolve_targets(args)
    if not targets:
        print("Nothing to extract.")
        return 0

    ok = 0
    for row in targets:
        print(f"\n[extract] {row['id']}")
        result = extract_recipe(row, dry_run=args.dry_run)
        if result is not None or args.dry_run:
            ok += 1
    print(f"\nDone: {ok}/{len(targets)}")
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
