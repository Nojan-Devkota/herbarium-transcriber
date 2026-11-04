import type { Check, Field } from "../api";
import { termLabel } from "./FieldRow";

const TOOL_NAMES: Record<string, string> = {
  grounding: "Grounding",
  gbif_name_match: "GBIF name match",
  date_check: "Date check",
  country_check: "Country check",
  reread_crop: "Re-read crop",
  agreement: "Second reading",
  geocode: "Georeference",
};

const str = (v: unknown) => (v === null || v === undefined ? "" : String(v));

function describe(c: Check): string {
  const r = c.result;
  if (r.error) return str(r.error);
  switch (c.tool) {
    case "grounding":
      return c.passed
        ? `Quoted text found in the transcript (score ${Number(r.score).toFixed(2)}).`
        : `Quoted text not found verbatim in the transcript (score ${Number(r.score).toFixed(2)}).`;
    case "gbif_name_match": {
      const base = `${str(r.matchType)} match, ${str(r.confidence)}% → ${str(r.scientificName) || "no name"}`;
      const status = r.status ? ` (${str(r.status).toLowerCase()})` : "";
      const accepted = r.acceptedName ? `. Accepted name: ${str(r.acceptedName)}` : "";
      return base + status + accepted;
    }
    case "date_check": {
      if (c.passed) return `Valid ${str(r.precision)}-precision date ${str(r.iso)}, consistent with the label.`;
      const problems = (r.problems as string[] | undefined)?.join("; ") ?? "failed";
      const alts = (r.alternatives as number[] | undefined)?.length ? ` Candidates: ${(r.alternatives as number[]).join(", ")}.` : "";
      return problems + "." + alts;
    }
    case "country_check":
      return c.passed ? `Resolves to ${str(r.name)} (${str(r.code)}).` : "Country not recognised.";
    case "reread_crop":
      return `Q: ${str(c.input.question)}\nA: “${str(r.answer)}”${r.legible === false ? " (not legible)" : ""} — ${str(r.note)}`;
    case "agreement":
      return `Independent second reading agrees ${Math.round(Number(r.score) * 100)}%.`;
    case "geocode":
      return c.passed ? `${str(r.decimalLatitude)}, ${str(r.decimalLongitude)} ± ${str(r.coordinateUncertaintyInMeters)} m — ${str(r.matched)}` : "No match.";
    default:
      return JSON.stringify(r);
  }
}

export default function AgentTrace({ field }: { field: Field | null }) {
  if (!field) return <div className="trace empty">Select a field to see how it was verified.</div>;
  const comps = (field.evidence.components ?? {}) as Record<string, unknown>;
  return (
    <div className="trace">
      <h3>
        Agent trace · <span>{termLabel(field.dwc_term)}</span>
      </h3>
      {field.checks.length === 0 ? (
        <p className="muted">
          {field.derived ? `Derived from ${str(field.evidence.derived_from)}: ${str(field.evidence.note)}` : "No checks recorded."}
        </p>
      ) : (
        <ol>
          {field.checks.map((c) => (
            <li key={c.id} className={`step ${c.passed === true ? "pass" : c.passed === false ? "fail" : "info"}`}>
              <span className="badge">{c.passed === true ? "✓" : c.passed === false ? "✗" : "?"}</span>
              <div>
                <strong>{TOOL_NAMES[c.tool] ?? c.tool}</strong>
                <p>{describe(c)}</p>
                <details>
                  <summary>raw</summary>
                  <pre>{JSON.stringify({ input: c.input, result: c.result }, null, 2)}</pre>
                </details>
              </div>
            </li>
          ))}
        </ol>
      )}
      {Object.keys(comps).length > 0 && (
        <p className="score-breakdown">
          Confidence {Math.round(field.confidence * 100)}% from{" "}
          {Object.entries(comps)
            .filter(([k]) => k !== "failed_checks")
            .map(([k, v]) => `${k.replace(/_/g, " ")} ${typeof v === "number" ? v.toFixed(2) : str(v)}`)
            .join(" · ")}
        </p>
      )}
    </div>
  );
}
