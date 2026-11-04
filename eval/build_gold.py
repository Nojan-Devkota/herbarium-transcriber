"""Pull and freeze the evaluation sets from GBIF.

GBIF already holds preserved-specimen records that have both an image and a
human transcription. We query a deliberate mix of countries and eras (printed
and handwritten labels, several languages), keep CC0 / CC-BY records that have
a scientific name, collector and date, download the images, and freeze:

    eval/gold.jsonl   — the held-out test set (never used for prompt tuning)
    eval/dev.jsonl    — a separate set for tuning prompts
    eval/images/      — the images (git-ignored; re-downloadable from image_url)

Usage:  python eval/build_gold.py --gold 50 --dev 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.pipeline.ingest import download_image, gbif_truth, is_open_license, normalize_license, normalize_image, pick_media  # noqa: E402
from app.tools.gbif import GbifClient  # noqa: E402

EVAL_DIR = ROOT / "eval"
IMAGES = EVAL_DIR / "images"
REQUIRED = ("scientificName", "recordedBy", "eventDate")

# A mix of regions, eras and label languages
QUERIES = [
    {"country": "US", "year": "1850,1920"},
    {"country": "US", "year": "1921,1980"},
    {"country": "NO", "year": "1850,1960"},
    {"country": "SE", "year": "1850,1960"},
    {"country": "FR", "year": "1820,1930"},
    {"country": "DE", "year": "1820,1930"},
    {"country": "MX", "year": "1880,1980"},
    {"country": "BR", "year": "1880,1980"},
    {"country": "AU", "year": "1850,1960"},
    {"country": "ZA", "year": "1850,1960"},
    {"country": "CA", "year": "1850,1960"},
    {"country": "GB", "year": "1820,1930"},
]


def candidates(gbif: GbifClient, per_query: int) -> list[dict]:
    out, seen = [], set()
    for q in QUERIES:
        body = gbif.occurrence_search(taxon_key=6, year=q["year"], limit=per_query * 4, extra={"country": q["country"]})
        kept = 0
        for occ in body.get("results") or []:
            key = str(occ.get("key"))
            media = pick_media(occ)
            if key in seen or media is None or any(not occ.get(f) for f in REQUIRED):
                continue
            lic = normalize_license(media[1])
            if not is_open_license(lic):
                continue
            seen.add(key)
            out.append({"key": key, "image_url": media[0], "license": lic, "query": q,
                        "institutionCode": occ.get("institutionCode"), "truth": gbif_truth(occ)})  # fmt: skip
            kept += 1
            if kept >= per_query:
                break
        print(f"  {q}: {kept} candidates", file=sys.stderr)
    return out


def fetch(http: httpx.Client, rec: dict) -> dict | None:
    path = IMAGES / f"{rec['key']}.jpg"
    try:
        data = path.read_bytes() if path.exists() else download_image(http, rec["image_url"])
        normalize_image(data)  # reject HTML error pages and broken files
    except (httpx.HTTPError, ValueError) as exc:
        print(f"  skip {rec['key']}: {type(exc).__name__}", file=sys.stderr)
        return None
    path.write_bytes(data)
    return {**rec, "image_file": f"images/{path.name}", "image_sha256": hashlib.sha256(data).hexdigest()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", type=int, default=50)
    ap.add_argument("--dev", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--force", action="store_true", help="overwrite existing frozen sets")
    args = ap.parse_args()

    if (EVAL_DIR / "gold.jsonl").exists() and not args.force:
        sys.exit("eval/gold.jsonl is frozen; pass --force to rebuild it (do not do this after tuning prompts)")
    IMAGES.mkdir(parents=True, exist_ok=True)
    need = args.gold + args.dev
    gbif = GbifClient()
    pool = candidates(gbif, per_query=max(3, (need * 2) // len(QUERIES) + 1))
    random.Random(args.seed).shuffle(pool)
    print(f"{len(pool)} candidates; downloading…", file=sys.stderr)

    http = httpx.Client(headers={"User-Agent": "herbarium-label-transcriber/0.1"})
    with ThreadPoolExecutor(max_workers=8) as ex:
        records = [r for r in ex.map(lambda r: fetch(http, r), pool) if r]
    if len(records) < need:
        print(f"warning: only {len(records)} usable records for {need} requested", file=sys.stderr)
    dev, gold = records[: args.dev], records[args.dev : args.dev + args.gold]
    for name, rows in (("dev", dev), ("gold", gold)):
        with open(EVAL_DIR / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"wrote eval/{name}.jsonl ({len(rows)} sheets)", file=sys.stderr)


if __name__ == "__main__":
    main()
