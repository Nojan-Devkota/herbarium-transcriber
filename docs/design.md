# Herbarium Label Transcriber — Design Doc

Nov 3, 2026 · @Nojan Devkota

## What this is

An agent that turns a photo of a pressed-plant herbarium sheet into a verified, standards-compliant museum record, using SAM 3.1 to find every label and Muse Spark to read, interpret and fact-check it.

**The problem.** Herbaria hold the physical evidence of where every plant species grew, and when, going back centuries. Most of that evidence is locked on paper:

- Less than 16% of the world's herbarium specimens have been imaged and shared, and the gaps are worst in the Global South ([Yale e360, on Kew's 2026 report](https://e360.yale.edu/digest/plants-fungi-conservation-science)).
- Even after a sheet is photographed, someone still has to type up its label. Transcription is often done by volunteers. At one garden it ran below the planned 10 records an hour because labels were long or in a foreign language ([NTBG, iDigBio](https://idigbio.org/wiki/images/2/20/2.Lorence_NTBG.pdf)).
- Labels are hard to read: 19th-century handwriting, Latin and German, abbreviations, stamps, and later re-identification slips glued on top.

**The product.** Upload one sheet or a whole batch. For each sheet you get a Darwin Core record (the data standard every museum and GBIF uses), with every field linked to the label region it came from, a confidence level, and the checks it passed. A curator reviews the flagged fields, then exports a CSV their collection database can import.

**Hackathon fit.** It addresses "Push the Frontier → accelerate a scientific experiment" and "Build for Your World." It is not a basic RAG app or a simple image analyzer: it segments, reads, reasons, verifies against outside sources, and measures its own accuracy.

## Who it helps and how

The direct user is a herbarium digitization team. Everyone downstream of their data benefits too.

| Who | Today | With this tool |
| --- | --- | --- |
| Digitization staff and volunteers | Type each label by hand; slow on old handwriting and foreign languages | Review a pre-filled record and fix only the flagged fields |
| Curators and collection managers | Have to choose between imaging a sheet and transcribing it | Can import transcribed records alongside the images |
| Small and Global South herbaria | No budget for large transcription projects | Pay per sheet in API calls, or run the open-weights model on their own hardware |
| Researchers (ecology, climate, conservation) | Can only use specimens that have been transcribed | More specimens become searchable by species, place and date |
| GBIF and aggregators | Receive records with inconsistent names and missing coordinates | Receive records with names matched to the GBIF taxonomy and coordinates with an uncertainty radius |

**Why the data matters.** Transcribed specimens are the raw material for science. Kew's 2026 report describes an AI analysis of eight million digitized specimens showing that flowering times shifted by about 2.5 days per decade over the past century ([SBS/Reuters](https://www.sbs.com.au/news/podcast-episode/digitisation-and-ai-being-used-to-chart-the-worlds-plant-and-fungi-population/kz7h66drz)). Every sheet still on paper is a data point that analyses like that can't use.

**The impact claim for judges.** The goal isn't to replace curators. It's to move them from typing to reviewing, which is the step where the cost per specimen can drop the most.

## Features

Six features are must-haves for the demo; everything else is cut unless time is left over.

**Must have (demo-critical)**

1. **Ingest:** upload images, or pull a batch from GBIF by dataset or species (preserved specimens with images).
2. **Region detection:** SAM 3.1 overlays for labels, annotation slips, barcodes, stamps, rulers and color charts, shown on the sheet.
3. **Verbatim transcription:** exact text per label crop, with line breaks, abbreviations and misspellings preserved.
4. **Darwin Core interpretation:** fields such as `scientificName`, `recordedBy`, `recordNumber`, `eventDate`, `country`, `stateProvince` and `locality`, each linked to its source crop.
5. **Verification agent:** checks the species name against the GBIF taxonomy and the date for plausibility, and re-reads the crop when a check fails.
6. **Review interface and export:** the sheet on one side, fields on the other with confidence colors; click a field to highlight its crop; accept or edit; download a Darwin Core CSV.

**Nice to have (only if ahead of schedule)**

- Georeferencing: turn the locality text into coordinates with an uncertainty radius, and show a map pin.
- Collector check: is the date inside the collector's active years?
- Identification history: keep each re-identification slip as its own entry instead of overwriting the name.
- Batch dashboard: throughput, cost per sheet, and the share of fields that needed human review.

**Explicitly out of scope:** identifying the plant from the specimen itself, accounts and authentication, and writing directly into collection databases such as Specify or Symbiota.

## Architecture

A six-stage pipeline runs in a background worker; each stage writes to SQLite and is cached, so the review interface reads finished results and never waits on a model.

&#91;embedded content: pipeline · 6 stages, 1 verification loop\]

The top row extracts the data and the bottom row checks it. The verification agent can loop back for a targeted re-read before anything reaches a person.

**Data model**

| Table | Key columns | Purpose |
| --- | --- | --- |
| Sheet | id, image\_sha256, source\_url, license, gbif\_occurrence\_id, status | One herbarium sheet and its rights |
| Region | id, sheet\_id, kind (label, slip, barcode, stamp), bbox, crop\_path | SAM 3.1 output; the anchor for provenance |
| Transcript | id, region\_id, text, model, prompt\_version, pass | Verbatim readings; two passes enable agreement scoring |
| Field | id, sheet\_id, dwc\_term, verbatim, value, region\_id, span, confidence, status | One Darwin Core value; status is auto, flagged, accepted or edited |
| Check | id, field\_id, tool, input, result, passed | Each verification step; drives the agent trace in the UI |
| Edit | id, field\_id, old\_value, new\_value, at | Human corrections; future training and evaluation data |

**API:** `POST /sheets` (upload), `POST /batches/gbif` (pull a batch), `GET /sheets/{id}` (sheet, regions, fields, checks), `PATCH /fields/{id}` (accept or edit), `GET /export.csv`.

## Engineering decisions

The through-line: every value is traceable to pixels, checked against something outside the model, and measured. Those three things are what separate this from "send the image to an LLM and ask for JSON."

| Decision | Choice | Why | Trade-off |
| --- | --- | --- | --- |
| Segment before reading | SAM 3.1 text-concept prompts ("label", "annotation slip", "barcode") crop each region; Spark reads the crops | Labels are a small part of a large sheet, so full-resolution crops read much better than the whole image; the crops also give each field its provenance | One extra model call per sheet; a missed region means missing fields, so the full sheet is kept as a fallback pass |
| Verbatim first, interpret second | Two separate Spark calls with separate schemas | Darwin Core has `verbatim*` fields for exactly this; a reading error and an interpretation error become separable and measurable | About twice the tokens per label |
| Provenance on every field | Each field stores `region_id`, crop bounding box and the source text span | Reviewers trust what they can see; the review interface highlights the source on click | Slightly heavier data model |
| Confidence from evidence, not self-report | Score = agreement between two readings + verification checks passed | Self-reported LLM confidence is poorly calibrated; agreement and external checks can be measured | Needs a second reading of fields that matter |
| Agentic verification loop | Tools: `gbif_name_match`, `date_check`, `geocode`, `reread_crop(question)`; at most 2 re-reads per field | Fixes misreads with a targeted question ("1897 or 1997?") instead of a full retry; the loop is bounded so cost stays predictable | More moving parts; every tool call is logged for the demo |
| Spend-aware model calls | Meta Model API only; a second Spark pass runs only on low-confidence fields | No local GPU or extra cloud accounts; stays inside the $150 credit; cost per sheet is a number to show judges | Not eligible for the open-weights bonus |
| Strict schemas | Pydantic models for every model output; reject and retry once on invalid JSON | Malformed output can't corrupt the database | One retry adds latency |
| Content-hash caching | Cache key = image SHA-256 + stage + prompt version | Re-runs during development cost nothing; the demo replays instantly | Cache has to be invalidated when prompts change (handled by the prompt version) |
| Boring stack | FastAPI, SQLite, background worker, React + Vite | One process to deploy; nothing new to learn tonight | Not built for scale, which is fine for a hackathon |
| Licensed data only | GBIF records filtered to CC0 / CC-BY images; license stored per sheet | The rules disqualify projects that use assets without the rights to them | A smaller pool of sheets to pick from |

## Evaluation plan

Show judges a field-level accuracy table with and without the verification agent; no other single artifact earns as much credibility.

**Ground truth for free.** GBIF already holds preserved-specimen records that have both an image and a human transcription. Query the occurrence API with `basisOfRecord=PRESERVED_SPECIMEN` and `mediaType=StillImage` ([GBIF API example](https://discourse.gbif.org/t/missing-species-media-in-api/4242)), keep CC0 or CC-BY records, and freeze 50 to 100 sheets as `eval/gold.jsonl`. Mix the set on purpose: printed and handwritten labels, at least one non-English label, and sheets with several labels.

**Metrics, per field**

- `scientificName`: exact match after GBIF name matching (a synonym of the correct accepted name counts as correct).
- `eventDate`: exact match at the precision the label gives (year, month or day).
- `recordedBy`, `locality`: normalized string similarity (character error rate), plus a pass rate at a fixed threshold.
- `country`, `stateProvince`: exact match.
- Calibration: accuracy of fields marked high confidence versus low. This proves the confidence colors mean something.
- Cost and time per sheet.

**Ablations to report:** (1) whole sheet straight to Spark, (2) + SAM crops, (3) + verification agent, (4) + selective second pass, with cost per sheet. Four rows is enough, and each one justifies a design choice.

**Honesty rules:** freeze the gold set before tuning prompts, tune on a separate 20-sheet dev set, and report the failure cases in the README.

## Repo structure

One public monorepo: a Python backend where each pipeline stage is its own module, a small React frontend, and the evaluation set and scripts.

```text
herbarium-transcriber/
├─ README.md                 # pitch, demo GIF, accuracy table, what was built during the hackathon
├─ .env.example              # META_API_KEY, MODEL_* names
├─ backend/
│  ├─ pyproject.toml
│  └─ app/
│     ├─ main.py              # FastAPI app, routes
│     ├─ config.py            # settings, model names, thresholds
│     ├─ db.py                # SQLite via SQLModel
│     ├─ models.py            # Sheet, Region, Transcript, Field, Check, Edit
│     ├─ schemas/
│     │  ├─ transcript.py     # verbatim output schema
│     │  └─ darwin_core.py    # interpreted field schema
│     ├─ clients/
│     │  ├─ base.py           # LLMClient interface
│     │  ├─ meta_api.py       # Muse Spark + SAM 3.1 via Meta Model API
│     │  └─ mock.py           # offline fake client for tests
│     ├─ pipeline/
│     │  ├─ ingest.py         # upload + GBIF fetch, license filter
│     │  ├─ segment.py        # SAM 3.1 concept prompts -> regions + crops
│     │  ├─ transcribe.py     # verbatim reading per crop
│     │  ├─ interpret.py      # verbatim -> Darwin Core
│     │  ├─ verify.py         # agent loop, bounded re-reads
│     │  ├─ confidence.py     # agreement + checks -> score
│     │  └─ run.py            # orchestrates stages, caching, job status
│     ├─ tools/
│     │  ├─ gbif.py           # name match, occurrence search
│     │  ├─ dates.py          # parsing + plausibility
│     │  └─ geocode.py        # locality -> lat/lon + uncertainty (nice to have)
│     ├─ prompts/              # versioned prompt files (v1_transcribe.md, ...)
│     ├─ export/dwc_csv.py     # Darwin Core CSV export
│     └─ cache.py              # sha256(image)+stage+prompt_version
├─ frontend/
│  ├─ package.json
│  └─ src/
│     ├─ pages/Upload.tsx
│     ├─ pages/Review.tsx      # sheet viewer + field panel
│     ├─ pages/Batch.tsx       # progress, cost, accuracy
│     ├─ components/SheetCanvas.tsx   # image + region overlays
│     ├─ components/FieldRow.tsx      # value, confidence, source highlight
│     └─ components/AgentTrace.tsx    # the verification log, shown in the demo
├─ eval/
│  ├─ build_gold.py           # pull + freeze the GBIF ground-truth set
│  ├─ gold.jsonl / dev.jsonl
│  ├─ run_eval.py             # runs the 4 ablations
│  └─ report.md               # generated accuracy table
├─ scripts/seed_demo.py         # pre-runs demo sheets into the cache
└─ tests/                       # schema, date parsing, GBIF tool, export
```

The structure is shaped by four choices: stages are pure functions from inputs to outputs, so each can be cached and tested on its own; prompts are versioned files, not strings in code; the Meta API sits behind one `LLMClient` interface; and `AgentTrace.tsx` exists because the verification log is the most impressive thing to show on screen.

## Build plan, demo and risks

About 21 hours remain before the 9:00 PM ET Wednesday deadline. The plan below includes 4 hours of sleep and finishes recording the demo with an hour of buffer. Times are ET.

**Build plan**

1. **Now:** claim the $150 Meta API credits (same 9 PM deadline) and set a spend limit on the Billing page.
2. **00:00–01:30 Setup:** repo scaffold, the Meta API client, one working SAM 3.1 call and one Spark call.
3. **01:30–04:00 Core pipeline from the command line:** segment, transcribe, interpret on 5 sheets; build `gold.jsonl` in parallel.
4. **04:00–08:00** Sleep.
5. **08:00–10:30 Verification agent:** GBIF name match, date check, bounded re-read, confidence scoring.
6. **10:30–13:30 Review interface:** sheet canvas with overlays, field panel, source highlight, agent trace, CSV export.
7. **13:30–15:30 Evaluation:** run the 4 ablations, fix the single worst failure mode, regenerate `report.md`.
8. **15:30–17:00 Polish:** seed demo sheets into the cache, README with the accuracy table, deploy.
9. **17:00–19:30 Demo video:** record and edit 3 minutes.
10. **19:30–20:30 Submit:** repo public, video link, team listed, post on X for the Most Viral prize.

**Cut rules:** if the verification agent isn't working by 11:00, ship it with only the GBIF name check. If the interface runs late, drop the Batch page.

**Demo script (3:00)**

- **0:00–0:20 Hook:** the backlog in one sentence, over a shot of herbarium sheets.
- **0:20–1:30 One 1890s sheet, live:** SAM overlays, verbatim text, Darwin Core fields; then the agent catches an outdated name or a misread year, re-reads the crop and fixes it, with the trace visible.
- **1:30–2:05 Review:** click a field to light up its source, fix one flagged value, export the CSV.
- **2:05–2:40 Proof:** the batch run, the ablation table, cost per sheet.
- **2:40–3:00 Built during the hackathon:** the list of what was built in-event, and the repo link.

**Risks**

| Risk | Fallback |
| --- | --- |
| SAM 3.1 through the Meta API is slow or limited | Ask Spark for label bounding boxes and crop from those; SAM stays the default path |
| Spend approaches the $150 credit | Billing spend limit; cache every call; evaluate on 50 sheets instead of 100 |
| Handwriting accuracy is low | Lean on confidence flags and the review step; report the real numbers |
| GBIF API rate limits | Cache every response; pull the gold set once, early |
| Live demo breaks | Demo sheets pre-run into the cache; the video is recorded from a known-good run |
