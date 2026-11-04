import { useCallback, useState } from "react";
import type { SheetSummary } from "../api";
import { api, isFinished } from "../api";
import { usePolling } from "../hooks";

const LICENSES = [
  { value: "CC0_1_0", label: "CC0 1.0 (public domain)" },
  { value: "CC_BY_4_0", label: "CC BY 4.0" },
  { value: "", label: "Own image / not stated" },
];

export default function Upload() {
  const load = useCallback(() => api.listSheets(), []);
  const { data: sheets, error, refresh } = usePolling<SheetSummary[]>(load, 3000);
  const [files, setFiles] = useState<File[]>([]);
  const [license, setLicense] = useState("CC0_1_0");
  const [sourceUrl, setSourceUrl] = useState("");
  const [species, setSpecies] = useState("");
  const [dataset, setDataset] = useState("");
  const [years, setYears] = useState("1850,1950");
  const [limit, setLimit] = useState(5);
  const [message, setMessage] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);

  const doUpload = async () => {
    if (!files.length) return;
    setBusy(true);
    try {
      const res = await api.upload(files, license, sourceUrl);
      const bad = res.filter((r) => r.error);
      setMessage(`Uploaded ${res.length - bad.length} sheet(s)` + (bad.length ? `; ${bad.map((b) => `${b.filename}: ${b.error}`).join(", ")}` : ""));
      setFiles([]);
      refresh();
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  };

  const doGbif = async () => {
    setBusy(true);
    setMessage("Fetching openly licensed specimens from GBIF…");
    try {
      const res = await api.gbifBatch({
        scientific_name: species || undefined,
        dataset_key: dataset || undefined,
        year: years || undefined,
        limit,
      });
      setMessage(`GBIF: ${res.added.length} added, ${res.existing.length} already here, ${res.skipped.length} skipped`);
      refresh();
    } catch (e) {
      setMessage(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page upload">
      <section className="intro">
        <h1>From pressed plant to verified museum record</h1>
        <p>
          Upload herbarium sheet photos. SAM 3.1 finds every label, Muse Spark reads it verbatim and maps it to Darwin Core, and a
          verification agent checks names against GBIF and dates for plausibility. You review only what is flagged.
        </p>
      </section>

      <div className="ingest">
        <div
          className={`card dropzone ${dragging ? "drag" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            setFiles([...files, ...Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith("image/"))]);
          }}
        >
          <h2>Upload sheets</h2>
          <label className="file-pick">
            <input type="file" accept="image/*" multiple onChange={(e) => setFiles(Array.from(e.target.files ?? []))} />
            <span>{files.length ? `${files.length} file(s) selected` : "Drop images here or click to choose"}</span>
          </label>
          <div className="row">
            <label>
              Licence
              <select value={license} onChange={(e) => setLicense(e.target.value)}>
                {LICENSES.map((l) => (
                  <option key={l.value} value={l.value}>
                    {l.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Source URL
              <input value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} placeholder="optional" />
            </label>
          </div>
          <button disabled={busy || !files.length} onClick={doUpload}>
            Upload &amp; process
          </button>
        </div>

        <div className="card">
          <h2>Pull a batch from GBIF</h2>
          <p className="muted small">Preserved specimens with images, CC0 / CC BY only.</p>
          <div className="row">
            <label>
              Species
              <input value={species} onChange={(e) => setSpecies(e.target.value)} placeholder="e.g. Botrychium lunaria" />
            </label>
            <label>
              or dataset key
              <input value={dataset} onChange={(e) => setDataset(e.target.value)} placeholder="GBIF dataset UUID" />
            </label>
          </div>
          <div className="row">
            <label>
              Years
              <input value={years} onChange={(e) => setYears(e.target.value)} placeholder="1850,1950" />
            </label>
            <label>
              Sheets
              <input type="number" min={1} max={50} value={limit} onChange={(e) => setLimit(Number(e.target.value))} />
            </label>
          </div>
          <button disabled={busy || (!species && !dataset)} onClick={doGbif}>
            Fetch &amp; process
          </button>
        </div>
      </div>

      {message && <div className="notice">{message}</div>}
      {error && <div className="error-box">API unreachable: {error}</div>}

      <h2>Sheets</h2>
      {!sheets?.length ? (
        <p className="muted">No sheets yet.</p>
      ) : (
        <div className="sheet-grid">
          {sheets.map((s) => (
            <a key={s.id} className="sheet-card" href={`#/sheets/${s.id}`}>
              <div className="thumb" style={{ backgroundImage: `url(${api.imageUrl(s.id)})` }} />
              <div className="sheet-card-body">
                <strong>
                  <i>{s.scientific_name ?? s.filename ?? `Sheet ${s.id}`}</i>
                </strong>
                <span className={`pill pill-${s.status}`}>{s.status === "processing" ? s.stage ?? "processing" : s.status}</span>
                {isFinished(s.status) && s.status === "done" && (
                  <span className="muted small">
                    {s.fields_total} fields · {s.fields_flagged} flagged · ${s.cost_usd.toFixed(3)}
                  </span>
                )}
                {s.status === "error" && <span className="small error-text">{s.error}</span>}
              </div>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
