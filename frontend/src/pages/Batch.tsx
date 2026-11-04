import { useCallback } from "react";
import type { Stats } from "../api";
import { api } from "../api";
import { usePolling } from "../hooks";

const pct = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

function Bar({ parts }: { parts: { label: string; value: number; cls: string }[] }) {
  const total = parts.reduce((s, p) => s + p.value, 0) || 1;
  return (
    <div className="bar" role="img" aria-label={parts.map((p) => `${p.label} ${p.value}`).join(", ")}>
      {parts.map((p) =>
        p.value ? (
          <span key={p.label} className={p.cls} style={{ width: `${(p.value / total) * 100}%` }} title={`${p.label}: ${p.value}`}>
            {p.value / total > 0.08 ? `${p.label} ${p.value}` : ""}
          </span>
        ) : null,
      )}
    </div>
  );
}

export default function Batch() {
  const load = useCallback(() => api.stats(), []);
  const { data: s, error } = usePolling<Stats>(load, (st) =>
    (st?.sheets_by_status.queued ?? 0) + (st?.sheets_by_status.processing ?? 0) > 0 ? 4000 : null,
  );
  if (error && !s) return <div className="page error-box" role="alert">{error}</div>;
  if (!s) return <div className="page muted">Loading…</div>;

  const done = s.sheets_by_status.done ?? 0;
  const conf = s.fields_by_confidence;
  const st = s.fields_by_status;
  return (
    <div className="page batch">
      <h1>Batch dashboard</h1>
      {error && (
        <div className="error-box" role="alert">
          Showing the last numbers; refresh failed: {error}
        </div>
      )}
      <div className="stat-grid">
        <div className="stat">
          <span className="big">
            {done}/{s.sheets_total}
          </span>
          <span>sheets processed</span>
        </div>
        <div className="stat">
          <span className="big">{s.avg_cost_per_sheet_usd === null ? "—" : `$${s.avg_cost_per_sheet_usd.toFixed(3)}`}</span>
          <span>average cost per sheet</span>
        </div>
        <div className="stat">
          <span className="big">{s.avg_model_seconds_per_sheet === null ? "—" : `${s.avg_model_seconds_per_sheet}s`}</span>
          <span>model time per sheet</span>
        </div>
        <div className="stat">
          <span className="big">{pct(s.share_needing_review)}</span>
          <span>of fields need human review</span>
        </div>
        <div className="stat">
          <span className="big">${s.total_cost_usd.toFixed(2)}</span>
          <span>
            total spend · {s.cached_calls}/{s.model_calls} calls from cache
          </span>
        </div>
      </div>

      <div className="card">
        <h2>Fields by confidence</h2>
        <Bar
          parts={[
            { label: "high", value: conf.high ?? 0, cls: "seg-high" },
            { label: "medium", value: conf.medium ?? 0, cls: "seg-medium" },
            { label: "low", value: conf.low ?? 0, cls: "seg-low" },
          ]}
        />
        <h2>Review progress</h2>
        <Bar
          parts={[
            { label: "auto", value: st.auto ?? 0, cls: "seg-auto" },
            { label: "flagged", value: st.flagged ?? 0, cls: "seg-flagged" },
            { label: "accepted", value: st.accepted ?? 0, cls: "seg-accepted" },
            { label: "edited", value: st.edited ?? 0, cls: "seg-edited" },
          ]}
        />
      </div>

      <div className="card">
        <h2>Calibration from review</h2>
        <p className="muted small">
          Share of reviewed fields a curator had to change, by the confidence the pipeline gave them. If the colours mean something,
          high-confidence fields should rarely be edited.
        </p>
        <table>
          <thead>
            <tr>
              <th>Confidence</th>
              <th>Fields</th>
              <th>Edited by reviewer</th>
            </tr>
          </thead>
          <tbody>
            {(["high", "medium", "low"] as const).map((lvl) => (
              <tr key={lvl}>
                <td>
                  <span className={`conf conf-${lvl}`}>{lvl}</span>
                </td>
                <td>{conf[lvl] ?? 0}</td>
                <td>{pct(s.edit_rate_by_confidence[lvl])}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">
          Field-level accuracy against GBIF ground truth, with and without the verification agent, is produced offline by{" "}
          <code>eval/run_eval.py</code> → <code>eval/report.md</code>.
        </p>
      </div>
      <p>
        <a className="button" href={api.exportUrl()}>
          Download Darwin Core CSV
        </a>{" "}
        <a className="button ghost" href={api.exportUrl(true)}>
          Fully reviewed sheets only
        </a>
      </p>
    </div>
  );
}
