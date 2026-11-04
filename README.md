# Herbarium Label Transcriber

**Photo of a pressed-plant herbarium sheet in, verified Darwin Core museum record out.**
SAM 3.1 finds every label on the sheet, Muse Spark reads each one verbatim and maps it to
Darwin Core, and a bounded verification agent checks the result against GBIF before a
curator reviews only what is flagged.

Fewer than 16% of the world's herbarium specimens are imaged and shared, and even imaged
sheets still need someone to type up their labels: 19th-century handwriting, Latin,
abbreviations, re-identification slips glued on top. This tool moves digitisation staff
from *typing* to *reviewing*.

> Design doc: [docs/design.md](docs/design.md) · Accuracy report: [eval/report.md](eval/report.md)

## What it does

1. **Ingest** — upload images, or pull a batch of openly licensed (CC0 / CC BY) preserved
   specimens from GBIF by species or dataset. Licence is stored per sheet.
2. **Segment** — SAM 3.1 concept prompts (`label`, `annotation slip`, `barcode`, `stamp`,
   `ruler`, `color chart`) find every region; text regions are cropped at full resolution.
   If SAM finds nothing, Spark proposes boxes; failing that, the whole sheet is read.
3. **Transcribe** — Muse Spark reads each crop *verbatim*: abbreviations, misspellings and
   line breaks preserved.
4. **Interpret** — a separate Spark call maps the transcripts to Darwin Core terms
   (`scientificName`, `recordedBy`, `recordNumber`, `eventDate`, `country`,
   `stateProvince`, `locality`, …). Every field quotes the exact text it came from, so it
   links back to a character span in a specific crop.
5. **Verify** — an agent runs tools per field: GBIF Backbone name match (synonyms → accepted
   name), date plausibility and consistency, country lookup. When a check fails it asks
   Spark a *targeted* question about the crop ("is the year 1897 or 1891?"), at most twice
   per field. Every step is logged and shown in the UI.
6. **Confidence & review** — confidence comes from evidence, not model self-report:
   grounding + external checks + agreement between two independent readings (the second
   reading runs only for fields that aren't already high-confidence). The curator reviews
   flagged fields side by side with the sheet and exports a Darwin Core CSV.

```
 upload / GBIF ──► SAM 3.1 regions ──► verbatim reading ──► Darwin Core interpretation
                                                                     │
       CSV export ◄── human review ◄── confidence ◄── verification agent ◄┘
                                          ▲                 │  ▲
                                          └─ 2nd reading ◄──┘  └─ targeted re-read (≤2)
```

## Results

Field-level accuracy on 50 frozen GBIF sheets, with each design choice added in turn.
See [eval/report.md](eval/report.md) for CER, calibration and every failure case.

<!-- RESULTS:START -->
| Pipeline | scientificName | eventDate | recordedBy | locality | country | stateProvince | Overall | Cost / sheet | Model time / sheet | Fields flagged |
|---|---|---|---|---|---|---|---|---|---|---|
| 1. Whole sheet → Spark | 78% | 48% | 72% | 59% | 56% | 50% | **62%** | $0.022 | 31 s | 100% |
| 2. + SAM 3.1 crops | 74% | 50% | 76% | 59% | 56% | 50% | **63%** | $0.054 | 75 s | 100% |
| 3. + verification agent | 74% | 50% | 76% | 59% | 56% | 50% | **63%** | $0.057 | 79 s | 65% |
| 4. + selective 2nd reading | 74% | 50% | 76% | 59% | 56% | 50% | **63%** | $0.073 | 102 s | 6% |

Calibration of the full pipeline (accuracy by assigned confidence):

| Confidence | Fields | Accuracy |
|---|---|---|
| high | 183 | 78% |
| medium | 5 | 60% |
| low | 19 | 58% |
| missing | 44 | 0% |

**What this shows, honestly:**

- **~63% of scored fields match GBIF's human transcription**, with scientific names at
  74–78% and collectors at 76%. Many misses are not misreadings: 44 of the 94 wrong fields
  are fields the label never states (22 are `country`, which GBIF infers from the
  locality), and GBIF often normalises names and localities. See the failure table in
  [eval/report.md](eval/report.md).
- **SAM 3.1 crops did not raise accuracy on this set** (+1 point, at 2.5× the cost). The gold
  set is mostly high-resolution Kew scans where a 2048 px whole sheet is still legible. On
  this set, the crops' value is provenance: every field links to its exact source region.
- **The verification agent mostly triages rather than corrects.** It leaves accuracy flat
  but routes 35% of fields to "auto" with external evidence.
- **The second reading is over-confident.** Spark re-reading the same crop usually agrees
  with itself, so the full pipeline marks most fields high-confidence (only 6% flagged).
  High-confidence fields are more accurate than low (78% vs 58%), but not by enough. The
  next step is a more independent second reading (whole sheet vs crop, or a different
  model setting), and inferring `country` from the province or locality. Both should be
  tuned on the dev set, not on gold.

The full evaluation (4 ablations × 50 sheets) cost **$4.73** in API calls with the shared cache.
<!-- RESULTS:END -->

## Quick start

Requirements: Python 3.11+, Node 20+, a [Meta Model API](https://dev.meta.ai/docs) key.

```bash
git clone https://github.com/Nojan-Devkota/herbarium-transcriber.git
cd herbarium-transcriber
cp .env.example .env          # then put your key in META_API_KEY
```

Backend:

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest                        # 97 offline tests, no key needed
uvicorn app.main:app --reload # http://127.0.0.1:8000
```

Frontend (dev server with hot reload, proxies the API):

```bash
cd frontend
npm install
npm run dev                   # http://localhost:5173
```

Or build it once and let FastAPI serve everything on :8000:

```bash
cd frontend && npm run build
```

No key? Set `LLM_CLIENT=mock` in `.env` to run the whole app on a deterministic offline
client.

### Demo and evaluation

```bash
python scripts/seed_demo.py                 # pre-run dev-set sheets into the cache
python eval/build_gold.py                   # (already done) freeze gold/dev sets from GBIF
python eval/run_eval.py --set gold          # 4 ablations -> eval/report.md
```

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sheets` | Upload one or more images (multipart: `files`, `license`, `source_url`, `process`) |
| `POST` | `/batches/gbif` | Pull openly licensed specimens: `{scientific_name \| dataset_key \| taxon_key, year, limit}` |
| `GET` | `/sheets` | Sheet summaries with status, name, flagged count, cost |
| `GET` | `/sheets/{id}` | Regions, transcripts, fields with their verification trace, GBIF ground truth |
| `POST` | `/sheets/{id}/process` | Re-run, optionally as an ablation (`full`, `sam+verify`, `sam-crops`, `whole-sheet`) |
| `PATCH` | `/fields/{id}` | Review: `{"action": "accept"}` or `{"action": "edit", "value": "…"}` |
| `GET` | `/export.csv` | Darwin Core CSV (`?reviewed_only=true` for fully reviewed sheets) |
| `GET` | `/stats` | Throughput, cost per sheet, share needing review, edit rate per confidence level |

Interactive docs at `/docs` when the backend is running.

## Engineering decisions

- **Segment before reading.** Labels are a small part of a large sheet; full-resolution
  crops read better than the whole image and give every field its provenance.
- **Verbatim first, interpret second.** Two calls with two strict schemas, so reading
  errors and interpretation errors are separable and measurable.
- **Provenance on every field.** Region, bounding box and source character span; the UI
  highlights the source when you click a field.
- **Confidence from evidence.** Grounding, external checks and two-reading agreement, not the
  model's self-reported confidence. The calibration table checks that it means something.
- **Bounded agent.** At most two targeted re-reads per field and the loop stops as soon as the
  model repeats itself, so cost per sheet stays predictable.
- **Strict schemas.** Every model output is validated with Pydantic; invalid JSON is retried
  once, then the sheet fails cleanly instead of corrupting the database.
- **Content-hash caching.** `sha256(image) + stage + prompt version + inputs`; prompts are
  versioned files in `backend/app/prompts/`, so editing one invalidates only its stage.
  Re-runs and demos cost nothing.
- **Licensed data only.** GBIF batches keep CC0 / CC BY images; licence is stored per sheet
  and exported.

## Repository layout

```
backend/app/
  main.py              FastAPI routes          config.py   settings, models, thresholds
  models.py, db.py     SQLite via SQLModel     cache.py    content-hash cache
  clients/             LLMClient interface, Meta Model API (Spark + SAM 3.1), offline mock
  pipeline/            ingest, segment, transcribe, interpret, verify, confidence, run (+ worker)
  tools/               GBIF name match & occurrence search, date parsing, country/geocode
  prompts/             versioned prompt files
  schemas/             Pydantic schemas for model outputs and the API
  export/dwc_csv.py    Darwin Core CSV
frontend/src/          React + Vite: Upload, Review (canvas, fields, agent trace), Batch
eval/                  build_gold.py, gold/dev.jsonl (frozen), metrics.py, run_eval.py, report.md
scripts/seed_demo.py   pre-run demo sheets into the cache
tests/                 97 offline tests (schemas, tools, pipeline, API, export, metrics)
```

## Built during the hackathon

Everything in this repository: the pipeline, verification agent, confidence scoring, API,
review interface, evaluation sets and harness. Models: Muse Spark 1.3 and SAM 3.1 via the
Meta Model API. Data: GBIF (CC0 / CC BY records only).

## Data and licences

Specimen images and transcriptions come from GBIF publishers under CC0 or CC BY 4.0; each
sheet records its licence and source URL, and the gold/dev sets list them per record.
Evaluation images are not stored in git (`eval/images/`); each record's `image_url` points
to the original.
