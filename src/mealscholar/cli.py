"""Command-line front end for the MealScholar workflow."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn
from dotenv import find_dotenv, load_dotenv

from .config import USER_AGENT, Settings, load_settings
from .context import NominatimPlaces
from .diary import Diary
from .insights import LOW_DV_PCT, period_report, summarize_days
from .nutrients import BY_KEY, NUTRIENTS
from .nutrition import FoodDatabase, FoodDatabaseMissing, build_database
from .photos import PhotoStore
from .pipeline import UnknownFood, Workflow, correct
from .schemas import MealResult, Range
from .vision import GeminiVision, VisionError
from .web import create_app

_TIME_SOURCE = {"exif": "photo time", "device": "device time", "upload": "upload time"}


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    load_dotenv(find_dotenv(usecwd=True))  # .env never overrides variables already set
    try:
        settings = load_settings()
        args.run(args, settings)
    except (FoodDatabaseMissing, VisionError, UnknownFood, ValueError, OSError, httpx.HTTPError) as exc:
        # OSError: missing or unreadable photo; httpx.HTTPError: USDA download failed.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mealscholar", description="MealScholar: photo -> foods -> USDA nutrients.")
    sub = parser.add_subparsers(required=True, metavar="command")

    p = sub.add_parser("fdc-import", help="download USDA FoodData Central and build the local database")
    p.set_defaults(run=cmd_fdc_import)

    p = sub.add_parser("search", help="search the local USDA database (find ids for `fix --food`)")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(run=cmd_search)

    p = sub.add_parser("analyze", help="analyze a meal photo and log it")
    p.add_argument("photo", type=Path)
    p.add_argument("--taken-at", type=datetime.fromisoformat, help="device capture time (ISO 8601)")
    p.add_argument("--lat", type=float, help="device latitude at capture (opt-in)")
    p.add_argument("--lon", type=float, help="device longitude at capture (opt-in)")
    p.add_argument("--use-photo-location", action="store_true", help="opt in to the photo's GPS tags")
    p.add_argument("--answer", help="answer to the clarifying question (skips the prompt)")
    p.add_argument("--no-log", action="store_true", help="don't save to the diary")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p.set_defaults(run=cmd_analyze)

    p = sub.add_parser("show", help="show a logged meal")
    p.add_argument("meal_id", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_show)

    p = sub.add_parser("fix", help="correct an item's grams or USDA match")
    p.add_argument("meal_id", type=int)
    p.add_argument("item", type=int, help="item number as shown by `show`")
    p.add_argument("--grams", type=int)
    p.add_argument("--food", type=int, metavar="FDC_ID", help="switch to another USDA food")
    p.add_argument("--json", action="store_true")
    p.set_defaults(run=cmd_fix)

    p = sub.add_parser("delete", help="remove a logged meal from the diary")
    p.add_argument("meal_id", type=int)
    p.set_defaults(run=cmd_delete)

    p = sub.add_parser("day", help="totals for one day")
    p.add_argument("day", nargs="?", type=date.fromisoformat, default=None)
    p.set_defaults(run=cmd_day)

    p = sub.add_parser("week", help="7-day averages and micronutrient trends")
    p.add_argument("--end", type=date.fromisoformat, default=None)
    p.set_defaults(run=cmd_week)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from a phone on the same network")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(run=cmd_serve)
    return parser


# --- commands ---------------------------------------------------------------


def cmd_fdc_import(args: argparse.Namespace, settings: Settings) -> None:
    counts = build_database(settings.food_db, settings.downloads)
    total = sum(counts.values())
    detail = ", ".join(f"{name}: {n:,}" for name, n in counts.items())
    print(f"Imported {total:,} foods into {settings.food_db} ({detail}).")


def cmd_search(args: argparse.Namespace, settings: Settings) -> None:
    found = FoodDatabase(settings.food_db).search(args.query, limit=args.limit)
    if found.foods and not found.exact:
        print("(loose: no food contains every word before the first comma; closest results)")
    for food in found.foods:
        kcal = food.per_100g.get("energy_kcal")
        energy = f"{kcal:.0f} kcal/100 g" if kcal is not None else "no energy data"
        print(f"{food.fdc_id:>8}  {food.description}  [{food.data_type}; {energy}]")


def cmd_analyze(args: argparse.Namespace, settings: Settings) -> None:
    if (args.lat is None) != (args.lon is None):
        raise ValueError("--lat and --lon go together.")
    if not settings.gemini_api_key:
        raise ValueError("Set GEMINI_API_KEY (create one at https://aistudio.google.com/apikey).")
    diary = Diary(settings.diary_db)
    workflow = Workflow(
        vision=GeminiVision(settings.gemini_api_key, settings.gemini_model, settings.thinking_level),
        foods=FoodDatabase(settings.food_db),
        places=NominatimPlaces(settings.nominatim_url, USER_AGENT),
    )
    result = workflow.analyze(
        args.photo,
        device_time=args.taken_at,
        device_location=(args.lat, args.lon) if args.lat is not None else None,
        read_photo_gps=args.use_photo_location,
        usual_portions=diary.usual_portions(),
    )
    if result.question:
        answer = args.answer
        if answer is None and not args.json and sys.stdin.isatty():
            print(_format_meal(result))
            answer = input(f"\n{result.question}\nYour answer (Enter to skip): ").strip()
            print()
        if answer:
            result = workflow.answer(result, answer)
    if result.items and not args.no_log:
        result = diary.save(result)
    print(result.model_dump_json(indent=2) if args.json else _format_meal(result))


def cmd_show(args: argparse.Namespace, settings: Settings) -> None:
    result = _logged_meal(Diary(settings.diary_db), args.meal_id)
    print(result.model_dump_json(indent=2) if args.json else _format_meal(result))


def cmd_fix(args: argparse.Namespace, settings: Settings) -> None:
    if args.grams is None and args.food is None:
        raise ValueError("Pass --grams and/or --food.")
    if args.grams is not None and args.grams <= 0:
        raise ValueError("--grams must be positive.")
    diary = Diary(settings.diary_db)
    result = _logged_meal(diary, args.meal_id)
    if not 1 <= args.item <= len(result.items):
        raise ValueError(f"Meal #{args.meal_id} has items 1-{len(result.items)}.")
    corrected = correct(result, args.item - 1, FoodDatabase(settings.food_db), grams=args.grams, fdc_id=args.food)
    result = diary.save(corrected)
    if args.grams is not None:
        diary.remember_portion(result.items[args.item - 1].item.name, args.grams)
    print(result.model_dump_json(indent=2) if args.json else _format_meal(result))


def cmd_delete(args: argparse.Namespace, settings: Settings) -> None:
    if not Diary(settings.diary_db).delete(args.meal_id):
        raise ValueError(f"No meal #{args.meal_id}.")
    PhotoStore(settings.photos).delete(args.meal_id)  # kept when the web app logged it
    print(f"Deleted meal #{args.meal_id}.")


def cmd_day(args: argparse.Namespace, settings: Settings) -> None:
    day = args.day or date.today()
    summaries = summarize_days(Diary(settings.diary_db).meals_between(day, day))
    if not summaries:
        print(f"No meals logged on {day}.")
        return
    summary = summaries[0]
    print(f"{day:%a %Y-%m-%d}: {len(summary.meals)} meal(s)")
    for meal in summary.meals:
        kcal = meal.totals.get("energy_kcal")
        names = ", ".join(r.item.name for r in meal.items)
        energy = _fmt_range(kcal, "kcal") if kcal is not None else "-"
        print(f"  #{meal.id:<4} {meal.context.meal_slot:<9} {meal.context.taken_at:%H:%M}  {energy}  {names}")
    print("\nDay total")
    print(_format_nutrients(summary.totals, summary.daily_value_pct))


def cmd_week(args: argparse.Namespace, settings: Settings) -> None:
    last = args.end or date.today()
    first = last - timedelta(days=6)
    report = period_report(first, last, Diary(settings.diary_db).meals_between(first, last))
    print(
        f"{first} to {last}: {report.days_logged} of 7 days logged, {report.meals_logged} meal(s). "
        "Averages cover logged days only."
    )
    if not report.days_logged:
        return
    for trend in report.trends:
        n = BY_KEY[trend.key]
        line = f"  {n.label:<16} {_fmt_number(trend.average_per_day, n.unit):>9} {n.unit}/day"
        if trend.average_dv_pct is not None:
            line += f"  ({trend.average_dv_pct:.0f}% DV)"
        if trend.low_days:
            line += f"  under {LOW_DV_PCT:.0f}% DV on {trend.low_days} of {report.days_logged} days"
        print(line)
    print("Micronutrients are photo-based estimates; use the trend, not a single day.")


def cmd_serve(args: argparse.Namespace, settings: Settings) -> None:
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


# --- formatting ---------------------------------------------------------------


def _logged_meal(diary: Diary, meal_id: int) -> MealResult:
    result = diary.get(meal_id)
    if result is None:
        raise ValueError(f"No meal #{meal_id}.")
    return result


def _format_meal(result: MealResult) -> str:
    ctx = result.context
    header = "Meal" if result.id is None else f"Meal #{result.id}"
    lines = [f"{header} · {ctx.meal_slot} · {ctx.taken_at:%a %Y-%m-%d %H:%M} ({_TIME_SOURCE[ctx.time_source]})"]
    if ctx.place:
        cuisine = f", {ctx.place.cuisine}" if ctx.place.cuisine else ""
        lines.append(f"Place: {ctx.place.name} ({ctx.place.kind.replace('_', ' ')}{cuisine})")
    if result.scale_reference:
        lines.append(f"Size judged against: {result.scale_reference}")
    lines.append("")
    for i, r in enumerate(result.items, 1):
        item = r.item
        grams = f"{item.grams_likely} g"
        if item.grams_low != item.grams_high:
            grams += f" ({item.grams_low}-{item.grams_high})"
        extra = ", likely hidden oil/butter" if item.hidden_fat_likely else ""
        lines.append(f"{i:>3}. {item.name}: {grams}, {item.cooking_method.replace('_', ' ')}{extra}")
        if r.match:
            lines.append(f"     USDA: {r.match.description} [{r.match.data_type} #{r.match.fdc_id}]")
            if (kcal := r.nutrients.get("energy_kcal")) is not None:
                lines.append(f"     {_fmt_range(kcal, 'kcal')}")
        else:
            lines.append("     USDA: no close match, not counted")
            for alt in r.alternatives[:3]:
                lines.append(f"       closest: {alt.description} (fix with --food {alt.fdc_id})")
    if result.items:
        lines += ["", "Total", _format_nutrients(result.totals, result.daily_value_pct)]
    if result.partial_nutrients:
        labels = ", ".join(BY_KEY[k].label for k in result.partial_nutrients)
        lines.append(f"  * no USDA data for some foods, totals undercount: {labels}")
    if result.answer:
        lines.append(f"\nYou answered: {result.answer}")
    elif result.question:
        lines.append(f"\nOpen question: {result.question}")
    notes = [*ctx.notes, *result.notes]
    if notes:
        lines.append("\nNotes:")
        lines += [f"  - {note}" for note in notes]
    return "\n".join(lines)


def _format_nutrients(totals: dict[str, Range], dv: dict[str, Range]) -> str:
    def rows(micro: bool) -> Iterable[str]:
        for n in NUTRIENTS:
            if n.micro != micro or n.key not in totals:
                continue
            pct = f"{dv[n.key].likely:>4.0f}% DV" if n.key in dv else ""
            yield f"  {n.label:<16} {_fmt_range(totals[n.key], n.unit):<28} {pct}"

    lines = list(rows(micro=False))
    micro = list(rows(micro=True))
    if micro:
        lines.append("  Micronutrients (estimates; weekly trends are more reliable):")
        lines += micro
    return "\n".join(lines)


def _fmt_range(r: Range, unit: str) -> str:
    likely = _fmt_number(r.likely, unit)
    low, high = _fmt_number(r.low, unit), _fmt_number(r.high, unit)
    return f"{likely} {unit}" if low == high else f"{likely} {unit} ({low}-{high})"


def _fmt_number(value: float, unit: str) -> str:
    if unit == "kcal" or value >= 100:
        return f"{value:,.0f}"
    return f"{value:.1f}"
