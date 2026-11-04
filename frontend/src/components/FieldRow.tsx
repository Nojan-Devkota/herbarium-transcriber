import { useEffect, useRef, useState } from "react";
import type { Field } from "../api";

const TERM_LABELS: Record<string, string> = {
  catalogNumber: "Catalogue number",
  institutionCode: "Institution",
  scientificName: "Scientific name",
  acceptedNameUsage: "Accepted name (GBIF)",
  taxonID: "GBIF taxon key",
  family: "Family",
  recordedBy: "Collector",
  recordNumber: "Collector number",
  eventDate: "Collection date",
  country: "Country",
  countryCode: "Country code",
  stateProvince: "State / province",
  county: "County",
  municipality: "Municipality",
  locality: "Locality",
  habitat: "Habitat",
  verbatimElevation: "Elevation (verbatim)",
  verbatimCoordinates: "Coordinates (verbatim)",
  decimalLatitude: "Latitude",
  decimalLongitude: "Longitude",
  coordinateUncertaintyInMeters: "Uncertainty (m)",
  identifiedBy: "Identified by",
  dateIdentified: "Date identified",
  occurrenceRemarks: "Remarks",
};

export const termLabel = (term: string) => TERM_LABELS[term] ?? term;

interface Props {
  field: Field;
  selected: boolean;
  onSelect: () => void;
  onReview: (action: "accept" | "edit", value?: string) => Promise<void>;
}

export default function FieldRow({ field, selected, onSelect, onReview }: Props) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(field.value ?? "");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => setDraft(field.value ?? ""), [field.value]);
  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  const run = async (action: "accept" | "edit", value?: string) => {
    setBusy(true);
    try {
      await onReview(action, value);
      setEditing(false);
    } finally {
      setBusy(false);
    }
  };

  const reviewed = field.status === "accepted" || field.status === "edited";
  const changedByAgent = field.model_value !== null && field.value !== field.model_value && field.status !== "edited";

  return (
    <li
      className={`field-row level-${field.confidence_level} ${selected ? "selected" : ""} ${reviewed ? "reviewed" : ""}`}
      onClick={onSelect}
      aria-selected={selected}
    >
      <div className="field-head">
        <span className="term" title={field.dwc_term}>
          {termLabel(field.dwc_term)}
        </span>
        <span className={`conf conf-${field.confidence_level}`} title="Confidence from evidence (agreement + checks)">
          {field.confidence_level} · {Math.round(field.confidence * 100)}%
        </span>
        <span className={`status status-${field.status}`}>{field.status}</span>
      </div>

      {editing ? (
        <form
          className="field-edit"
          onSubmit={(e) => {
            e.preventDefault();
            run("edit", draft);
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <input ref={inputRef} value={draft} onChange={(e) => setDraft(e.target.value)} disabled={busy} />
          <button type="submit" disabled={busy}>
            Save
          </button>
          <button type="button" className="ghost" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </form>
      ) : (
        <div className="field-value">{field.value || <em>empty</em>}</div>
      )}

      <div className="field-meta">
        {field.derived ? (
          <span className="derived">derived by {String(field.evidence.derived_from ?? "tool")} check</span>
        ) : (
          field.verbatim && (
            <span className="verbatim" title="Exact text on the label">
              “{field.verbatim}”
            </span>
          )
        )}
        {changedByAgent && <span className="agent-fix">agent corrected from “{field.model_value}”</span>}
      </div>

      {!editing && (
        <div className="field-actions" onClick={(e) => e.stopPropagation()}>
          <button disabled={busy || field.status === "accepted"} onClick={() => run("accept")} title="Accept (a)">
            ✓ Accept
          </button>
          <button className="ghost" disabled={busy} onClick={() => setEditing(true)} title="Edit (e)">
            Edit
          </button>
        </div>
      )}
    </li>
  );
}
