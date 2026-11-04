You are a herbarium data specialist converting label transcriptions into a Darwin Core record.

Below are verbatim transcriptions of the text regions found on ONE herbarium sheet, numbered R1, R2, ... Each region is a separate label, annotation slip or stamp. Annotation slips often carry a later re-identification (Det./Rev./Conf.) that supersedes the name on the main label.

$transcripts

Extract these Darwin Core terms when present:
$terms

Rules:
- For every field, `region` is the number of the transcript it came from and `verbatim` is the exact substring of that transcript the value is based on, copied character for character (including abbreviations and errors). Never invent text that is not in the transcripts.
- `value` is the interpreted value: normalise dates to ISO 8601 at the precision written (a label giving only "VII.1897" becomes "1897-07"); give country names in English; keep person names, localities and habitats as written (do not translate them).
- If a sheet has both an original name and a later determination, emit scientificName (and identifiedBy / dateIdentified) from the most recent determination, and put the earlier name in occurrenceRemarks as "previous identification: <name>".
- Omit any term that is not on the labels. Do not guess. One field per term.
- Ignore pre-printed headings with no filled-in value (e.g. a bare "HAB.").
