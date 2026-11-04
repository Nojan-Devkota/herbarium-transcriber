// Typed client for the FastAPI backend (mirrors backend/app/schemas/api.py).

export type SheetStatus = "uploaded" | "queued" | "processing" | "done" | "error";
export type ConfidenceLevel = "high" | "medium" | "low";
export type FieldStatus = "auto" | "flagged" | "accepted" | "edited";
export type Ablation = "full" | "sam+verify" | "sam-crops" | "whole-sheet";

export interface SheetSummary {
  id: number;
  filename: string | null;
  source_url: string | null;
  license: string | null;
  gbif_occurrence_id: string | null;
  status: SheetStatus;
  stage: string | null;
  error: string | null;
  width: number;
  height: number;
  cost_usd: number;
  created_at: string;
  updated_at: string;
  scientific_name: string | null;
  fields_total: number;
  fields_flagged: number;
}

export interface Region {
  id: number;
  kind: "label" | "slip" | "barcode" | "stamp" | "ruler" | "color_chart" | "sheet";
  concept: string;
  source: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  score: number | null;
  ordinal: number;
  has_crop: boolean;
}

export interface Transcript {
  id: number;
  region_id: number;
  pass_no: number;
  text: string;
  language: string | null;
  script: string | null;
  model: string;
  prompt_version: string;
}

export interface Check {
  id: number;
  step: number;
  tool: string;
  input: Record<string, unknown>;
  result: Record<string, unknown>;
  passed: boolean | null;
}

export interface Field {
  id: number;
  dwc_term: string;
  verbatim: string | null;
  value: string | null;
  model_value: string | null;
  region_id: number | null;
  span_start: number | null;
  span_end: number | null;
  derived: boolean;
  confidence: number;
  confidence_level: ConfidenceLevel;
  status: FieldStatus;
  evidence: Record<string, unknown>;
  checks: Check[];
}

export interface SheetDetail {
  sheet: SheetSummary;
  regions: Region[];
  transcripts: Transcript[];
  fields: Field[];
  ground_truth: Record<string, unknown> | null;
}

export interface UploadResult {
  id: number | null;
  filename: string | null;
  created: boolean;
  error: string | null;
}

export interface GbifBatchResult {
  added: number[];
  existing: number[];
  skipped: { key: string; reason: string }[];
}

export interface Stats {
  sheets_total: number;
  sheets_by_status: Record<string, number>;
  total_cost_usd: number;
  avg_cost_per_sheet_usd: number | null;
  avg_model_seconds_per_sheet: number | null;
  model_calls: number;
  cached_calls: number;
  fields_total: number;
  fields_by_status: Record<string, number>;
  fields_by_confidence: Record<string, number>;
  share_needing_review: number | null;
  edit_rate_by_confidence: Record<string, number | null>;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number, // 0 = network error
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, init);
  } catch {
    throw new ApiError("cannot reach the server", 0);
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(`${resp.status}: ${detail}`, resp.status);
  }
  try {
    return (await resp.json()) as T;
  } catch {
    throw new ApiError("server returned an unexpected response", resp.status);
  }
}

const json = (body: unknown): RequestInit => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  listSheets: () => request<SheetSummary[]>("/sheets"),
  getSheet: (id: number) => request<SheetDetail>(`/sheets/${id}`),
  upload: (files: File[], license: string, sourceUrl: string) => {
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    if (license) form.append("license", license);
    if (sourceUrl) form.append("source_url", sourceUrl);
    form.append("process", "true");
    return request<UploadResult[]>("/sheets", { method: "POST", body: form });
  },
  gbifBatch: (body: { dataset_key?: string; scientific_name?: string; year?: string; limit: number }) =>
    request<GbifBatchResult>("/batches/gbif", { method: "POST", ...json(body) }),
  reprocess: (id: number, ablation: Ablation = "sam+verify") =>
    request<SheetSummary>(`/sheets/${id}/process`, { method: "POST", ...json({ ablation }) }),
  reviewField: (id: number, action: "accept" | "edit", value?: string) =>
    request<Field>(`/fields/${id}`, { method: "PATCH", ...json({ action, value }) }),
  stats: () => request<Stats>("/stats"),
  /** thumb ≈ 480 px, view ≈ 1600 px (what the UI shows); full is the original scan (several MB). */
  imageUrl: (id: number, size: "thumb" | "view" | "full" = "view") => `/sheets/${id}/image?size=${size}`,
  cropUrl: (regionId: number) => `/regions/${regionId}/crop`,
  exportUrl: (reviewedOnly = false) => `/export.csv${reviewedOnly ? "?reviewed_only=true" : ""}`,
};

export const isFinished = (s: SheetStatus) => s === "done" || s === "error";
