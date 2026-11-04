"""Run the four ablations on a frozen set and write eval/report.md.

Ablations (design doc):
  1. whole-sheet  — whole sheet straight to Spark (no SAM, no verification)
  2. sam-crops    — + SAM 3.1 crops
  3. sam+verify   — + verification agent (GBIF / dates / country, bounded re-reads)
  4. full         — + selective second reading for low-confidence fields

Each ablation gets its own database under eval/runs/<set>/<ablation>/, while all
share one model-call cache (eval/runs/cache), so identical calls are paid for
once. Cost and model time per sheet use each call's *nominal* cost, i.e. what it
cost when first made, so cached replays don't make an ablation look free.

Usage:
  python eval/run_eval.py --set gold                 # all four ablations, 50 sheets
  python eval/run_eval.py --set dev --limit 5        # quick check
  python eval/run_eval.py --client mock --limit 3    # offline smoke test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

from sqlmodel import select  # noqa: E402

from app import config, db  # noqa: E402
from app.cache import Cache  # noqa: E402
from app.models import Field, ModelCall, Sheet, SheetStatus  # noqa: E402
from app.pipeline.context import RunOptions  # noqa: E402
from app.pipeline.ingest import ingest_image  # noqa: E402
from app.pipeline.run import process_sheet  # noqa: E402
from app.tools.gbif import GbifClient  # noqa: E402
from eval.metrics import EVAL_FIELDS, score_sheet  # noqa: E402

ABLATIONS: dict[str, RunOptions] = {
    "whole-sheet": RunOptions(use_sam=False, use_verify=False, use_second_pass=False),
    "sam-crops": RunOptions(use_verify=False, use_second_pass=False),
    "sam+verify": RunOptions(use_second_pass=False),
    "full": RunOptions(use_second_pass=True),
}
DESCRIPTIONS = {
    "whole-sheet": "1. Whole sheet → Spark",
    "sam-crops": "2. + SAM 3.1 crops",
    "sam+verify": "3. + verification agent",
    "full": "4. + selective 2nd reading",
}


def load_set(name: str, limit: int | None) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in open(ROOT / "eval" / f"{name}.jsonl", encoding="utf-8")]
    return rows[:limit] if limit else rows


def use_data_dir(run_dir: Path, cache_dir: Path, client: str) -> None:
    os.environ["DATA_DIR"] = str(run_dir)
    os.environ["CACHE_DIR"] = str(cache_dir)
    os.environ["LLM_CLIENT"] = client
    config.get_settings.cache_clear()
    db.reset_engine()


def run_ablation(name: str, rows: list[dict[str, Any]], args: argparse.Namespace, cache_dir: Path) -> dict[str, Any]:
    run_dir = ROOT / "eval" / "runs" / args.set / name
    use_data_dir(run_dir, cache_dir, args.client)
    ids: dict[str, int] = {}
    with db.session_scope() as s:
        for r in rows:
            data = (ROOT / "eval" / r["image_file"]).read_bytes()
            sheet, _ = ingest_image(s, data, filename=r["image_file"], source_url=r["image_url"], license=r["license"],
                                    gbif_occurrence_id=r["key"], gbif_record=r["truth"])  # fmt: skip
            ids[r["key"]] = sheet.id

    todo = [sid for sid in ids.values() if args.rerun or _status(sid) != SheetStatus.done]
    t0 = time.time()
    print(f"[{name}] processing {len(todo)}/{len(ids)} sheets with {args.workers} workers", file=sys.stderr)

    def work(sid: int) -> None:
        try:
            sheet = process_sheet(sid, options=ABLATIONS[name])
        except Exception as exc:  # one bad sheet must not abort the whole evaluation
            print(f"  [{name}] sheet {sid}: crashed {type(exc).__name__}: {exc}", file=sys.stderr)
            return
        print(f"  [{name}] sheet {sid}: {sheet.status.value} ${sheet.cost_usd:.4f}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, todo))
    wall = time.time() - t0

    gbif = GbifClient(cache=Cache(cache_dir))
    results: list[dict[str, Any]] = []
    per_sheet: list[dict[str, Any]] = []
    with db.session_scope() as s:
        for r in rows:
            sid = ids[r["key"]]
            sheet = s.get(Sheet, sid)
            fields = s.exec(select(Field).where(Field.sheet_id == sid)).all()
            pred = {f.dwc_term: {"value": f.value, "verbatim": f.verbatim, "confidence_level": f.confidence_level} for f in fields}
            calls = s.exec(select(ModelCall).where(ModelCall.sheet_id == sid)).all()
            # A sheet that failed to process is reported, not scored as if every field were wrong
            rows_scored = score_sheet(pred, r["truth"], gbif.name_match) if sheet.status == SheetStatus.done else []
            for x in rows_scored:
                x["key"] = r["key"]
            results += rows_scored
            per_sheet.append({
                "key": r["key"], "status": sheet.status.value, "error": sheet.error,
                "nominal_cost_usd": sum(c.nominal_cost_usd for c in calls),
                "nominal_model_s": sum(c.nominal_latency_ms for c in calls) / 1000,
                "fields": len(fields), "flagged": sum(1 for f in fields if f.status.value == "flagged"),
            })  # fmt: skip
    return {"ablation": name, "results": results, "sheets": per_sheet, "wall_s": round(wall, 1)}


def _status(sid: int) -> SheetStatus:
    with db.session_scope() as s:
        return s.get(Sheet, sid).status


def summarize(run: dict[str, Any]) -> dict[str, Any]:
    by_field: dict[str, list[bool]] = defaultdict(list)
    cers: dict[str, list[float]] = defaultdict(list)
    for x in run["results"]:
        by_field[x["field"]].append(x["correct"])
        if x["cer"] is not None:
            cers[x["field"]].append(x["cer"])
    sheets = run["sheets"]
    n = len(sheets) or 1
    all_correct = [x["correct"] for x in run["results"]]
    total_fields = sum(s["fields"] for s in sheets) or 1
    return {
        "accuracy": {f: (sum(v) / len(v), len(v)) for f, v in by_field.items()},
        "overall": sum(all_correct) / len(all_correct) if all_correct else 0.0,
        "cer": {f: sum(v) / len(v) for f, v in cers.items()},
        "cost": sum(s["nominal_cost_usd"] for s in sheets) / n,
        "model_s": sum(s["nominal_model_s"] for s in sheets) / n,
        "flagged_share": sum(s["flagged"] for s in sheets) / total_fields,
        "errors": sum(1 for s in sheets if s["status"] != "done"),
        "scored_sheets": sum(1 for s in sheets if s["status"] == "done"),
    }


def calibration(run: dict[str, Any]) -> dict[str, tuple[float, int]]:
    groups: dict[str, list[bool]] = defaultdict(list)
    for x in run["results"]:
        groups[x["confidence_level"]].append(x["correct"])
    return {lvl: (sum(v) / len(v), len(v)) for lvl, v in groups.items()}


def pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def write_report(runs: list[dict[str, Any]], args: argparse.Namespace, n_sheets: int) -> str:
    summaries = {r["ablation"]: summarize(r) for r in runs}
    lines = [
        "# Evaluation report",
        "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by `eval/run_eval.py` on the frozen **{args.set}** set "
        f"({n_sheets} sheets, client `{args.client}`). Ground truth is GBIF's human transcription of each specimen.",
        "",
        "## Field accuracy by ablation",
        "",
        "| Pipeline | " + " | ".join(EVAL_FIELDS) + " | Overall | Cost / sheet | Model time / sheet | Fields flagged | Failed sheets |",
        "|---|" + "---|" * (len(EVAL_FIELDS) + 5),
    ]
    for r in runs:
        s = summaries[r["ablation"]]
        cells = [pct(s["accuracy"][f][0]) if f in s["accuracy"] else "–" for f in EVAL_FIELDS]
        lines.append(
            f"| {DESCRIPTIONS[r['ablation']]} | " + " | ".join(cells)
            + f" | **{pct(s['overall'])}** | ${s['cost']:.3f} | {s['model_s']:.0f} s | {pct(s['flagged_share'])} | {s['errors']} |"
        )
    first = summaries[runs[0]["ablation"]]
    lines += ["", "Fields scored per column: " + ", ".join(f"{f} n={first['accuracy'][f][1]}" for f in EVAL_FIELDS if f in first["accuracy"]) + "."]
    lines += [
        "",
        "Metrics: scientificName counts as correct when it resolves to the same accepted GBIF taxon as the truth "
        "(synonyms of the right name are correct); eventDate must match at the precision GBIF records; recordedBy and "
        f"locality pass at token-set similarity ≥ {80}; country compares ISO codes; stateProvince ignores case and accents.",
        "",
        "## Character error rate (lower is better)",
        "",
        "| Pipeline | recordedBy CER | locality CER |",
        "|---|---|---|",
    ]
    for r in runs:
        s = summaries[r["ablation"]]
        lines.append(f"| {DESCRIPTIONS[r['ablation']]} | {s['cer'].get('recordedBy', 0):.2f} | {s['cer'].get('locality', 0):.2f} |")

    if any(r["ablation"] == "full" for r in runs):
        full = next(r for r in runs if r["ablation"] == "full")
        cal = calibration(full)
        lines += ["", "## Calibration (full pipeline)", "",
                  "Do the confidence colours mean something? Accuracy of scored fields by the confidence the pipeline assigned.", "",
                  "| Confidence | Fields | Accuracy |", "|---|---|---|"]  # fmt: skip
        for lvl in ("high", "medium", "low", "missing"):
            if lvl in cal:
                lines.append(f"| {lvl} | {cal[lvl][1]} | {pct(cal[lvl][0])} |")
        wrong = [x for x in full["results"] if not x["correct"]]
        lines += ["", f"## Failure cases (full pipeline, {len(wrong)} wrong fields)", "",
                  "| GBIF key | Field | Predicted | GBIF truth | Confidence |", "|---|---|---|---|---|"]  # fmt: skip
        for x in wrong[:25]:
            p = str(x["pred"] or "—").replace("|", "/")[:60]
            t = str(x["truth"]).replace("|", "/")[:60]
            lines.append(f"| [{x['key']}](https://www.gbif.org/occurrence/{x['key']}) | {x['field']} | {p} | {t} | {x['confidence_level']} |")
        errors = [s for s in full["sheets"] if s["status"] != "done"]
        if errors:
            lines += ["", "Sheets that failed to process: " + ", ".join(f"{s['key']} ({s['error']})" for s in errors)]
    lines += [
        "",
        "## Caveats",
        "",
        "- GBIF's transcription is not always the label text: collectors are often normalised (\"R. Sigafoos\" for "
        "\"Robert S. Sigafoos\"), localities are reformatted or merged with habitat, and some records were georeferenced "
        "or re-identified after transcription. String metrics therefore under-count correct readings; the failure table "
        "shows which misses are real.",
        "- The gold set leans on Kew (K) sheets; results on other herbaria's label styles may differ.",
        "- Cost uses each call's nominal price at Meta Model API list rates, including calls replayed from the shared cache.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", default="gold", choices=["gold", "dev"])
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--ablations", default=",".join(ABLATIONS))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--client", default="meta", choices=["meta", "mock"])
    ap.add_argument("--rerun", action="store_true", help="re-process sheets already done (cache still applies)")
    ap.add_argument("--out", default=None, help="report path (default eval/report.md, or eval/report_<set>.md for dev)")
    args = ap.parse_args()

    rows = load_set(args.set, args.limit)
    cache_dir = ROOT / "eval" / "runs" / "cache"
    runs = [run_ablation(name, rows, args, cache_dir) for name in args.ablations.split(",")]
    out_json = ROOT / "eval" / "runs" / f"results_{args.set}.json"
    out_json.write_text(json.dumps(runs, indent=1, ensure_ascii=False), encoding="utf-8")
    report = write_report(runs, args, len(rows))
    out = Path(args.out) if args.out else ROOT / "eval" / ("report.md" if args.set == "gold" else f"report_{args.set}.md")
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"wrote {out} and {out_json}", file=sys.stderr)
    failed = {r["ablation"]: summarize(r)["errors"] for r in runs}
    if any(failed.values()):
        print(f"warning: sheets that failed to process (excluded from accuracy): {failed}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
