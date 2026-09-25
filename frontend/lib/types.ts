/** Shape returned by POST /api/token and consumed by the client. */
export type ConnectionDetails = {
  serverUrl: string;
  roomName: string;
  participantToken: string;
  participantName: string;
};

/*
 * Call Analyzer contracts (v1). These mirror the analyzer's pydantic models; the
 * analyzer is the source of truth. `lib/analyzer.ts` checks the minimal shape at
 * runtime, and components treat every optional field defensively.
 */

export type Role = "user" | "assistant";

export type Turn = {
  id: string;
  role: Role;
  text: string;
  started_at: string | null;
  ended_at: string | null;
  interrupted: boolean;
  /** STT confidence 0..1 for caller turns; usually null for agent turns. */
  transcript_confidence: number | null;
};

export type ToolCall = {
  id: string;
  name: string;
  /** JSON-encoded arguments, exactly as the model produced them. */
  arguments: string;
  output: string | null;
  is_error: boolean;
  created_at: string | null;
};

export type CallRecord = {
  schema_version: number;
  call_id: string;
  room: string;
  agent_name: string;
  started_at: string;
  ended_at: string;
  duration_s: number;
  end_reason: string | null;
  prompt?: Partial<Record<"profile" | "fingerprint" | "version" | "voice" | "backend", string>>;
  models?: Partial<Record<"voice_model" | "voice" | "backend_model", string>>;
  turns: Turn[];
  tool_calls: ToolCall[];
};

export type AnalysisStatus = "pending" | "running" | "done" | "failed";
export type OutcomeStatus = "resolved" | "partially_resolved" | "unresolved" | "not_applicable";

export const DIMENSIONS = [
  "customer_satisfaction",
  "customer_frustration",
  "resolution",
  "agent_helpfulness",
  "accuracy_groundedness",
  "conversation_flow",
  "efficiency",
  "tone_empathy",
  "policy_adherence",
] as const;
export type Dimension = (typeof DIMENSIONS)[number];

export type Evidence = { turn_id: string; quote: string };
export type DimensionScore = { score: number; rationale: string; evidence: Evidence[] };

export type FlagType =
  | "escalation_needed"
  | "hallucination_risk"
  | "policy_violation"
  | "tool_failure"
  | "caller_repeated"
  | "long_silence"
  | "other";

export type Flag = { type: FlagType; turn_id: string | null; detail: string };

export type Metrics = {
  duration_s: number;
  turns: number;
  user_turns: number;
  agent_turns: number;
  talk_ratio_agent: number;
  interruptions: number;
  tool_calls: number;
  tool_errors: number;
  avg_agent_words_per_turn: number;
  mean_transcript_confidence: number | null;
  low_confidence_turns: number;
};

export type AnalyzerInfo = {
  provider: "openai" | "heuristic" | string;
  model: string | null;
  rubric_version: string;
};

export type Analysis = {
  call_id: string;
  status: AnalysisStatus;
  error: string | null;
  analyzer: AnalyzerInfo;
  created_at: string;
  // The fields below are only meaningful once status === "done".
  summary: string | null;
  caller_intent: string | null;
  outcome: { status: OutcomeStatus; reason: string } | null;
  overall_score: number | null;
  scores: Partial<Record<Dimension, DimensionScore>>;
  flags: Flag[];
  sentiment: { turn_id: string; value: number }[];
  metrics: Metrics | null;
};

/** One row of `GET /v1/calls`. */
export type CallSummary = {
  call_id: string;
  started_at: string;
  duration_s: number;
  caller_intent: string | null;
  summary: string | null;
  outcome: { status: OutcomeStatus; reason: string } | null;
  overall_score: number | null;
  /** Analysis status; null when the call has never been analyzed. */
  status: AnalysisStatus | null;
  turns: number;
  /** Who produced the analysis; null until one has been attempted. */
  analyzer: AnalyzerInfo | null;
};

export type CallPage = { items: CallSummary[]; next_cursor: string | null };

export type CallDetail = { record: CallRecord; analysis: Analysis | null };
