/**
 * Server-side client for the Call Analyzer service (see analyzer/README.md).
 *
 * The bearer token lives only in server env vars; `server-only` makes the build fail
 * if a Client Component ever imports this module. Every request has a timeout, and
 * every failure becomes an AnalyzerError with a message that is safe to show a user
 * (no URLs, tokens, or upstream bodies). Details go to the server log instead.
 */
import "server-only";
import { connection } from "next/server";
import type {
  Analysis,
  AnalysisStatus,
  AnalyzerInfo,
  CallDetail,
  CallPage,
  CallRecord,
  CallSummary,
} from "@/lib/types";

const TIMEOUT_MS = 8_000;
/** Calls per page on the Calls screen (first page and each "Load more"). */
const PAGE_SIZE = 12;

export type AnalyzerErrorKind =
  | "not_configured"
  | "unreachable"
  | "timeout"
  | "unauthorized"
  | "not_found"
  | "bad_request"
  | "upstream"
  | "bad_response";

const MESSAGES: Record<AnalyzerErrorKind, string> = {
  not_configured:
    "The call analyzer isn't configured. Set CALL_ANALYZER_URL and CALL_ANALYZER_TOKEN for the frontend.",
  unreachable: "Couldn't reach the call analyzer. Make sure it's running, then try again.",
  timeout: "The call analyzer took too long to respond. Try again in a moment.",
  unauthorized:
    "The call analyzer rejected this app's credentials. Check that CALL_ANALYZER_TOKEN matches on both sides.",
  not_found: "That call doesn't exist (or was deleted).",
  bad_request: "The call analyzer couldn't process that request.",
  upstream: "The call analyzer ran into a problem. Try again in a moment.",
  bad_response:
    "The call analyzer sent a response this app doesn't understand. Are both on the same version?",
};

export class AnalyzerError extends Error {
  constructor(readonly kind: AnalyzerErrorKind) {
    super(MESSAGES[kind]);
    this.name = "AnalyzerError";
  }
}

/** Friendly message for any error thrown while talking to the analyzer. */
export function analyzerErrorMessage(e: unknown): string {
  return e instanceof AnalyzerError ? e.message : MESSAGES.upstream;
}

/**
 * Call ids go into an upstream URL path, so only accept the analyzer's own call-id
 * alphabet (analyzer/call_analyzer/models.py `CallId`). The first character can't be
 * ".", which rules out "." / ".." path segments; encodeURIComponent is applied on top.
 */
const CALL_ID_RE = /^[A-Za-z0-9_][A-Za-z0-9._:@=+-]{0,199}$/;
export function isValidCallId(id: unknown): id is string {
  return typeof id === "string" && CALL_ID_RE.test(id);
}

/** Opaque pagination cursor from the analyzer; bounded and printable ASCII only. */
const CURSOR_RE = /^[\x21-\x7e]{1,512}$/;
export function isValidCursor(cursor: unknown): cursor is string {
  return typeof cursor === "string" && CURSOR_RE.test(cursor);
}

function config(): { base: string; token: string } {
  const url = process.env.CALL_ANALYZER_URL?.trim();
  const token = process.env.CALL_ANALYZER_TOKEN?.trim();
  if (!url || !token || !/^https?:\/\//i.test(url)) throw new AnalyzerError("not_configured");
  // Keep any path prefix on the base (e.g. https://host/analyzer), so no `new URL(path, base)`.
  return { base: url.replace(/\/+$/, ""), token };
}

async function request(path: string, init: { method?: "GET" | "POST" } = {}): Promise<unknown> {
  // Analyzer data is live: never let a page that reads it be prerendered at build time.
  await connection();
  const { base, token } = config();
  const method = init.method ?? "GET";
  let res: Response;
  try {
    res = await fetch(`${base}${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
      cache: "no-store",
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (e) {
    const timedOut = e instanceof DOMException && e.name === "TimeoutError";
    console.error(`[analyzer] ${method} ${path} failed:`, timedOut ? "timeout" : e);
    throw new AnalyzerError(timedOut ? "timeout" : "unreachable");
  }

  if (!res.ok) {
    console.error(`[analyzer] ${method} ${path} -> HTTP ${res.status}`);
    await res.body?.cancel();
    if (res.status === 401 || res.status === 403) throw new AnalyzerError("unauthorized");
    if (res.status === 404) throw new AnalyzerError("not_found");
    if (res.status >= 400 && res.status < 500) throw new AnalyzerError("bad_request");
    throw new AnalyzerError("upstream");
  }
  try {
    return await res.json();
  } catch {
    console.error(`[analyzer] ${method} ${path}: response was not JSON`);
    throw new AnalyzerError("bad_response");
  }
}

// ---------------------------------------------------------------------------
// Minimal runtime shape checks. They guard the fields the UI relies on to render
// at all; anything optional is defaulted so components never crash on a gap.

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

function invalid(what: string): never {
  console.error(`[analyzer] unexpected response shape: ${what}`);
  throw new AnalyzerError("bad_response");
}

function parseAnalyzerInfo(v: unknown): AnalyzerInfo | null {
  const provider = isObj(v) ? str(v.provider) : null;
  if (!isObj(v) || !provider) return null;
  return {
    provider,
    model: str(v.model),
    rubric_version: str(v.rubric_version) ?? "?",
  };
}

/** Keep only string values: these land in <code> elements as text. */
function strings<K extends string>(
  v: unknown,
  keys: readonly K[],
): Partial<Record<K, string>> | undefined {
  if (!isObj(v)) return undefined;
  const out: Partial<Record<K, string>> = {};
  for (const k of keys) {
    const s = str(v[k]);
    if (s !== null) out[k] = s;
  }
  return out;
}

const ANALYSIS_STATUSES: readonly AnalysisStatus[] = ["pending", "running", "done", "failed"];

/** An unknown status reads as "not analyzed" rather than breaking the row. */
function parseStatus(v: unknown): AnalysisStatus | null {
  return ANALYSIS_STATUSES.find((s) => s === v) ?? null;
}

function parseSummary(v: unknown): CallSummary {
  const callId = isObj(v) ? str(v.call_id) : null;
  if (!isObj(v) || !callId) invalid("list item without call_id");
  return {
    call_id: callId,
    started_at: str(v.started_at) ?? "",
    duration_s: num(v.duration_s) ?? 0,
    caller_intent: str(v.caller_intent),
    summary: str(v.summary),
    outcome: isObj(v.outcome) ? (v.outcome as CallSummary["outcome"]) : null,
    overall_score: num(v.overall_score),
    status: parseStatus(v.status),
    turns: num(v.turns) ?? 0,
    analyzer: parseAnalyzerInfo(v.analyzer),
  };
}

/**
 * The analyzer serves the record as the agent stored it, so every rendered field is
 * normalized here: a wrong type in one field degrades that field, not the whole page.
 */
function parseRecord(v: unknown): CallRecord {
  const callId = isObj(v) ? str(v.call_id) : null;
  if (!isObj(v) || !callId || !Array.isArray(v.turns)) invalid("record");
  // Empty text is valid (a turn interrupted before any words were transcribed).
  const turns = v.turns.filter((t): t is Obj => isObj(t) && !!str(t.id) && str(t.text) !== null);
  return {
    schema_version: num(v.schema_version) ?? 1,
    call_id: callId,
    room: str(v.room) ?? "",
    agent_name: str(v.agent_name) ?? "",
    started_at: str(v.started_at) ?? "",
    ended_at: str(v.ended_at) ?? "",
    duration_s: num(v.duration_s) ?? 0,
    end_reason: str(v.end_reason),
    prompt: strings(v.prompt, ["profile", "fingerprint", "version", "voice", "backend"] as const),
    models: strings(v.models, ["voice_model", "voice", "backend_model"] as const),
    turns: turns.map((t) => ({
      id: t.id as string,
      role: t.role === "user" ? "user" : "assistant",
      text: t.text as string,
      started_at: str(t.started_at),
      ended_at: str(t.ended_at),
      interrupted: t.interrupted === true,
      transcript_confidence: num(t.transcript_confidence),
    })),
    tool_calls: arr(v.tool_calls)
      .filter((c): c is Obj => isObj(c) && !!str(c.id) && !!str(c.name))
      .map((c) => ({
        id: c.id as string,
        name: c.name as string,
        arguments: str(c.arguments) ?? "",
        output: str(c.output),
        is_error: c.is_error === true,
        created_at: str(c.created_at),
      })),
  };
}

function parseAnalysis(v: unknown): Analysis | null {
  if (v === null || v === undefined) return null;
  if (!isObj(v) || !str(v.status)) invalid("analysis");
  return {
    ...(v as unknown as Analysis),
    analyzer: parseAnalyzerInfo(v.analyzer) ?? {
      provider: "unknown",
      model: null,
      rubric_version: "?",
    },
    scores: isObj(v.scores) ? (v.scores as Analysis["scores"]) : {},
    flags: arr(v.flags) as Analysis["flags"],
    sentiment: arr(v.sentiment).filter(
      (s): s is Analysis["sentiment"][number] => isObj(s) && num(s.value) !== null,
    ),
    metrics: isObj(v.metrics) ? (v.metrics as Analysis["metrics"]) : null,
    overall_score: num(v.overall_score),
  };
}

// ---------------------------------------------------------------------------
// Endpoints (analyzer `/v1` API).

export async function listCalls(opts: { cursor?: string } = {}): Promise<CallPage> {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (opts.cursor) {
    if (!isValidCursor(opts.cursor)) throw new AnalyzerError("bad_request");
    params.set("cursor", opts.cursor);
  }
  const body = await request(`/v1/calls?${params}`);
  if (!isObj(body) || !Array.isArray(body.items)) invalid("call list");
  return { items: body.items.map(parseSummary), next_cursor: str(body.next_cursor) };
}

export async function getCall(id: string): Promise<CallDetail> {
  if (!isValidCallId(id)) throw new AnalyzerError("not_found");
  const body = await request(`/v1/calls/${encodeURIComponent(id)}`);
  if (!isObj(body)) invalid("call detail");
  return { record: parseRecord(body.record), analysis: parseAnalysis(body.analysis) };
}

export async function reanalyzeCall(id: string): Promise<void> {
  if (!isValidCallId(id)) throw new AnalyzerError("not_found");
  await request(`/v1/calls/${encodeURIComponent(id)}/analyze`, { method: "POST" });
}
