# Evaluation report

Generated 2026-11-04 21:22 UTC by `eval/run_eval.py` on the frozen **gold** set (50 sheets, client `meta`). Ground truth is GBIF's human transcription of each specimen.

## Field accuracy by ablation

| Pipeline | scientificName | eventDate | recordedBy | locality | country | stateProvince | Overall | Cost / sheet | Model time / sheet | Fields flagged |
|---|---|---|---|---|---|---|---|---|---|---|
| 1. Whole sheet → Spark | 78% | 48% | 72% | 59% | 56% | 50% | **62%** | $0.022 | 31 s | 100% |
| 2. + SAM 3.1 crops | 74% | 50% | 76% | 59% | 56% | 50% | **63%** | $0.054 | 75 s | 100% |
| 3. + verification agent | 74% | 50% | 76% | 59% | 56% | 50% | **63%** | $0.057 | 79 s | 65% |
| 4. + selective 2nd reading | 74% | 50% | 76% | 59% | 56% | 50% | **63%** | $0.073 | 102 s | 6% |

Fields scored per column: scientificName n=50, eventDate n=50, recordedBy n=50, locality n=41, country n=50, stateProvince n=10.

Metrics: scientificName counts as correct when it resolves to the same accepted GBIF taxon as the truth (synonyms of the right name are correct); eventDate must match at the precision GBIF records; recordedBy and locality pass at token-set similarity ≥ 80; country compares ISO codes; stateProvince ignores case and accents.

## Character error rate (lower is better)

| Pipeline | recordedBy CER | locality CER |
|---|---|---|
| 1. Whole sheet → Spark | 0.63 | 0.47 |
| 2. + SAM 3.1 crops | 0.62 | 0.50 |
| 3. + verification agent | 0.62 | 0.50 |
| 4. + selective 2nd reading | 0.62 | 0.50 |

## Calibration (full pipeline)

Do the confidence colours mean something? Accuracy of scored fields by the confidence the pipeline assigned.

| Confidence | Fields | Accuracy |
|---|---|---|
| high | 183 | 78% |
| medium | 5 | 60% |
| low | 19 | 58% |
| missing | 44 | 0% |

## Failure cases (full pipeline, 94 wrong fields)

| GBIF key | Field | Predicted | GBIF truth | Confidence |
|---|---|---|---|---|
| [912525266](https://www.gbif.org/occurrence/912525266) | scientificName | Asplenium unilaterale Lam. | Asplenium L. | high |
| [912525266](https://www.gbif.org/occurrence/912525266) | country | — | US | missing |
| [574967961](https://www.gbif.org/occurrence/574967961) | scientificName | Borreria | Spermacoce brachystemonoides (Cham. & Schltdl.) Kuntze | low |
| [4947567402](https://www.gbif.org/occurrence/4947567402) | eventDate | — | 1930-01-01T00:00 | missing |
| [4947567402](https://www.gbif.org/occurrence/4947567402) | recordedBy | — | Sirjaev, G. | missing |
| [4947567402](https://www.gbif.org/occurrence/4947567402) | country | — | GB | missing |
| [1317461836](https://www.gbif.org/occurrence/1317461836) | country | — | US | missing |
| [5077872838](https://www.gbif.org/occurrence/5077872838) | scientificName | Calamintha silvatica Bromf. | Clinopodium calamintha (L.) Kuntze | high |
| [5077872838](https://www.gbif.org/occurrence/5077872838) | eventDate | 1930 | 1930-01-01T00:00 | high |
| [5077872838](https://www.gbif.org/occurrence/5077872838) | locality | heraa | [Meran.] | high |
| [4132073514](https://www.gbif.org/occurrence/4132073514) | stateProvince | — | Québec | missing |
| [1320712722](https://www.gbif.org/occurrence/1320712722) | eventDate | 1929-06-15 | 1930-01-21 | high |
| [1320712722](https://www.gbif.org/occurrence/1320712722) | recordedBy | G.V. | G. Vermeulen | high |
| [1320712722](https://www.gbif.org/occurrence/1320712722) | locality | Capelse Lei (Capel d. Rijn) | Erpeter Lei | medium |
| [4947607801](https://www.gbif.org/occurrence/4947607801) | locality | Ham Common, N.E. area, on bank at foot of wall of old shed | Surrey; [Hane Common], N. E. area | high |
| [4947607801](https://www.gbif.org/occurrence/4947607801) | country | — | GB | missing |
| [2512773340](https://www.gbif.org/occurrence/2512773340) | country | — | SE | missing |
| [2512773340](https://www.gbif.org/occurrence/2512773340) | stateProvince | Öland | Kalmar | high |
| [4981646064](https://www.gbif.org/occurrence/4981646064) | scientificName | Anthyllis Barba-Jovis | Astragalus L. | low |
| [4981646064](https://www.gbif.org/occurrence/4981646064) | eventDate | 1930 | 1930-01-01T00:00 | high |
| [5083026938](https://www.gbif.org/occurrence/5083026938) | eventDate | 1930-01 | 1930-01-01T00:00 | high |
| [5083026938](https://www.gbif.org/occurrence/5083026938) | locality | Railway bank St-maxime | [Railway bak St Maxime Vab.] | high |
| [912118327](https://www.gbif.org/occurrence/912118327) | eventDate | 1974-09-22 | 1980-01-01T00:00 | high |
| [4954009472](https://www.gbif.org/occurrence/4954009472) | eventDate | 1930 | 1930-01-01T00:00 | high |
| [4954009472](https://www.gbif.org/occurrence/4954009472) | recordedBy | — | Long, J.W. | missing |

## Caveats

- GBIF's transcription is not always the label text: collectors are often normalised ("R. Sigafoos" for "Robert S. Sigafoos"), localities are reformatted or merged with habitat, and some records were georeferenced or re-identified after transcription. String metrics therefore under-count correct readings; the failure table shows which misses are real.
- The gold set leans on Kew (K) sheets; results on other herbaria's label styles may differ.
- Cost uses each call's nominal price at Meta Model API list rates, including calls replayed from the shared cache.
