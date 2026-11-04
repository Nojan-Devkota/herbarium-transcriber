"""Pre-run demo sheets into the app database and cache, so the live demo replays instantly.

Sheets come from GBIF occurrence keys (default: a few from the dev set, never the
gold set) and are processed synchronously with the full pipeline. Re-running is
free: every model call is served from the cache.

Usage:
  python scripts/seed_demo.py                       # first 4 dev-set sheets
  python scripts/seed_demo.py --keys 1234 5678      # specific GBIF occurrences
  python scripts/seed_demo.py --species "Saxifraga oppositifolia" --n 3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import session_scope  # noqa: E402
from app.pipeline.ingest import download_image, fetch_gbif_batch, gbif_truth, ingest_image, is_open_license, normalize_license, pick_media  # noqa: E402
from app.pipeline.run import process_sheet  # noqa: E402
from app.tools.gbif import GbifClient  # noqa: E402


def ingest_keys(keys: list[str]) -> list[int]:
    gbif = GbifClient()
    http = httpx.Client(headers={"User-Agent": "herbarium-label-transcriber/0.1"})
    ids = []
    with session_scope() as s:
        for key in keys:
            occ = gbif.occurrence(key)
            media = pick_media(occ)
            lic = normalize_license(media[1]) if media else None
            if media is None or not is_open_license(lic):
                print(f"skip {key}: no openly licensed image", file=sys.stderr)
                continue
            local = ROOT / "eval" / "images" / f"{key}.jpg"
            data = local.read_bytes() if local.exists() else download_image(http, media[0])
            sheet, _ = ingest_image(s, data, filename=f"gbif-{key}.jpg", source_url=media[0], license=lic,
                                    gbif_occurrence_id=str(key), gbif_record=gbif_truth(occ))  # fmt: skip
            ids.append(sheet.id)
    return ids


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keys", nargs="*", help="GBIF occurrence keys")
    ap.add_argument("--species", help="pull sheets of this species from GBIF instead")
    ap.add_argument("--n", type=int, default=4)
    args = ap.parse_args()

    if args.species:
        with session_scope() as s:
            ids = fetch_gbif_batch(s, scientific_name=args.species, year="1850,1980", limit=args.n).added
    else:
        keys = args.keys or [json.loads(line)["key"] for line in open(ROOT / "eval" / "dev.jsonl", encoding="utf-8")][: args.n]
        ids = ingest_keys(keys)

    for sid in ids:
        sheet = process_sheet(sid)
        print(f"sheet {sid}: {sheet.status.value}  ${sheet.cost_usd:.4f}  {sheet.error or ''}")
    print("Start the app (uvicorn app.main:app) and open http://127.0.0.1:8000 to review them.")


if __name__ == "__main__":
    main()
