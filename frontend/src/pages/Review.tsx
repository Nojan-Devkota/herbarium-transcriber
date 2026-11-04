import { useCallback, useEffect, useMemo, useState } from "react";
import type { Ablation, Field, SheetDetail, Transcript } from "../api";
import { api, isFinished } from "../api";
import AgentTrace from "../components/AgentTrace";
import FieldRow from "../components/FieldRow";
import SheetCanvas from "../components/SheetCanvas";
import { usePolling } from "../hooks";

function Highlighted({ text, start, end }: { text: string; start: number | null; end: number | null }) {
  if (start === null || end === null || start >= end) return <pre className="transcript">{text}</pre>;
  return (
    <pre className="transcript">
      {text.slice(0, start)}
      <mark>{text.slice(start, end)}</mark>
      {text.slice(end)}
    </pre>
  );
}

const STAGES = ["segment", "transcribe", "interpret", "verify", "confidence"];

export default function Review({ sheetId }: { sheetId: number }) {
  const load = useCallback(() => api.getSheet(sheetId), [sheetId]);
  const [pollKey, setPollKey] = useState(0);
  // Poll while the sheet is being processed; stop once it is done (restarted by Re-run)
  const { data, error, setData } = usePolling<SheetDetail>(load, (d) => (d && isFinished(d.sheet.status) ? null : 2000), pollKey);
  const [selectedField, setSelectedField] = useState<number | null>(null);
  const [selectedRegion, setSelectedRegion] = useState<number | null>(null);
  const [filter, setFilter] = useState<"all" | "review">("all");
  const [ablation, setAblation] = useState<Ablation>("sam+verify");
  const [actionError, setActionError] = useState<string>();
  const [rerunning, setRerunning] = useState(false);

  const fields = useMemo(() => {
    const all = data?.fields ?? [];
    return filter === "review" ? all.filter((f) => f.status === "flagged") : all;
  }, [data, filter]);
  const field = data?.fields.find((f) => f.id === selectedField) ?? null;
  const regionId = field?.region_id ?? selectedRegion;
  const region = data?.regions.find((r) => r.id === regionId) ?? null;
  const transcriptFor = (pass: number): Transcript | undefined =>
    data?.transcripts.find((t) => t.region_id === regionId && t.pass_no === pass);

  const selectRegion = useCallback((id: number) => {
    setSelectedRegion(id);
    setSelectedField(null);
  }, []);

  const review = useCallback(
    async (f: Field, action: "accept" | "edit", value?: string) => {
      const updated = await api.reviewField(f.id, action, value);
      setData((d) => d && { ...d, fields: d.fields.map((x) => (x.id === updated.id ? updated : x)) });
    },
    [setData],
  );

  // Keyboard review: j/k (or arrows) move, a accepts
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement;
      if (["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(target.tagName) || target.isContentEditable) return;
      // Ctrl/Cmd+A must stay "select all"; holding a key must not fire repeated saves
      if (e.ctrlKey || e.metaKey || e.altKey || e.repeat || e.defaultPrevented) return;
      const idx = fields.findIndex((f) => f.id === selectedField);
      if (e.key === "j" || e.key === "ArrowDown") {
        const next = fields[Math.min(fields.length - 1, idx + 1)];
        if (next) setSelectedField(next.id);
        e.preventDefault();
      } else if (e.key === "k" || e.key === "ArrowUp") {
        const prev = fields[Math.max(0, idx - 1)];
        if (prev) setSelectedField(prev.id);
        e.preventDefault();
      } else if (e.key === "a" && field && field.status !== "accepted") {
        review(field, "accept").catch((err) => setActionError(err instanceof Error ? err.message : String(err)));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fields, selectedField, field, review]);

  if (error && !data)
    return (
      <div className="page error-box" role="alert">
        Could not load sheet {sheetId}: {error}
      </div>
    );
  if (!data) return <div className="page muted">Loading…</div>;

  const { sheet } = data;
  const reviewed = data.fields.filter((f) => f.status === "accepted" || f.status === "edited").length;
  const flagged = data.fields.filter((f) => f.status === "flagged").length;
  const p1 = transcriptFor(1);
  const p2 = transcriptFor(2);

  return (
    <div className="page review">
      <div className="review-header">
        <div>
          <a href="#/" className="back">
            ← All sheets
          </a>
          <h1>
            <i>{sheet.scientific_name ?? sheet.filename ?? `Sheet ${sheet.id}`}</i>
          </h1>
          <p className="muted">
            Sheet #{sheet.id} · {sheet.license ?? "licence unknown"}
            {sheet.gbif_occurrence_id && (
              <>
                {" · "}
                <a href={`https://www.gbif.org/occurrence/${sheet.gbif_occurrence_id}`} target="_blank" rel="noreferrer">
                  GBIF {sheet.gbif_occurrence_id}
                </a>
              </>
            )}
            {" · "}cost ${sheet.cost_usd.toFixed(4)}
          </p>
        </div>
        <div className="rerun">
          <select value={ablation} onChange={(e) => setAblation(e.target.value as Ablation)} aria-label="Pipeline variant">
            <option value="sam+verify">Standard pipeline</option>
            <option value="full">+ second reading (slower, costlier)</option>
            <option value="sam-crops">SAM crops only</option>
            <option value="whole-sheet">Whole sheet only</option>
          </select>
          <button
            className="ghost"
            disabled={!isFinished(sheet.status) || rerunning}
            onClick={async () => {
              if (reviewed > 0 && !window.confirm(`Re-running replaces this sheet's results, including ${reviewed} reviewed field(s). Continue?`)) return;
              setRerunning(true);
              setActionError(undefined);
              try {
                const summary = await api.reprocess(sheet.id, ablation);
                setSelectedField(null);
                setSelectedRegion(null);
                setData((d) => d && { ...d, sheet: summary, regions: [], transcripts: [], fields: [] });
                setPollKey((k) => k + 1); // restart polling until the re-run finishes
              } catch (err) {
                setActionError(err instanceof Error ? err.message : String(err));
              } finally {
                setRerunning(false);
              }
            }}
          >
            Re-run
          </button>
        </div>
      </div>

      {!isFinished(sheet.status) && (
        <div className="progress" role="status" aria-live="polite">
          {STAGES.map((s) => (
            <span
              key={s}
              className={`stage ${sheet.stage === s ? "current" : STAGES.indexOf(s) < STAGES.indexOf(sheet.stage ?? "") ? "done" : ""}`}
            >
              {s}
            </span>
          ))}
          <span className="muted">{sheet.status === "queued" ? "waiting in queue…" : "working…"}</span>
        </div>
      )}
      {sheet.status === "error" && (
        <div className="error-box" role="alert">
          Processing failed: {sheet.error}
        </div>
      )}
      {actionError && (
        <div className="error-box" role="alert">
          {actionError}
        </div>
      )}
      {error && (
        <div className="error-box" role="alert">
          Lost contact with the server: {error}
        </div>
      )}

      <div className="review-grid">
        <section className="left">
          <SheetCanvas
            sheetId={sheet.id}
            width={sheet.width}
            height={sheet.height}
            regions={data.regions}
            selectedRegionId={regionId}
            onSelectRegion={selectRegion}
          />
          {region && (
            <div className="source-panel">
              <div className="source-head">
                <strong>
                  {region.ordinal ? `R${region.ordinal} · ` : ""}
                  {region.kind}
                </strong>
                <span className="muted">
                  {region.source === "sam" ? `SAM 3.1 “${region.concept}”` : region.source}
                  {region.score != null && ` · ${Math.round(region.score * 100)}%`}
                </span>
              </div>
              {region.has_crop && <img className="crop" src={api.cropUrl(region.id)} alt="Label crop" />}
              {p1 && (
                <>
                  <h4>
                    Verbatim reading{" "}
                    <span className="muted">
                      · {p1.script}, {p1.language} · {p1.prompt_version}
                    </span>
                  </h4>
                  <Highlighted text={p1.text} start={field?.span_start ?? null} end={field?.span_end ?? null} />
                </>
              )}
              {p2 && (
                <details>
                  <summary>Independent second reading</summary>
                  <pre className="transcript">{p2.text}</pre>
                </details>
              )}
            </div>
          )}
        </section>

        <section className="right">
          <div className="fields-head">
            <h2>Darwin Core record</h2>
            <span className="muted">
              {reviewed}/{data.fields.length} reviewed · {flagged} flagged
            </span>
            <div className="seg">
              <button className={filter === "all" ? "on" : ""} aria-pressed={filter === "all"} onClick={() => setFilter("all")}>
                All
              </button>
              <button className={filter === "review" ? "on" : ""} aria-pressed={filter === "review"} onClick={() => setFilter("review")}>
                Needs review
              </button>
            </div>
          </div>
          {data.fields.length === 0 && isFinished(sheet.status) && <p className="muted">No fields were extracted.</p>}
          <ul className="fields">
            {fields.map((f) => (
              <FieldRow
                key={f.id}
                field={f}
                selected={f.id === selectedField}
                onSelect={() => setSelectedField(f.id)}
                onReview={(action, value) => review(f, action, value)}
              />
            ))}
          </ul>
          <AgentTrace field={field} />
          {data.ground_truth && (
            <details className="truth">
              <summary>GBIF human transcription (ground truth)</summary>
              <dl>
                {Object.entries(data.ground_truth).map(([k, v]) => (
                  <div key={k}>
                    <dt>{k}</dt>
                    <dd>{String(v)}</dd>
                  </div>
                ))}
              </dl>
            </details>
          )}
          <p className="hint muted">Keys: j/k move · a accept</p>
        </section>
      </div>
    </div>
  );
}
