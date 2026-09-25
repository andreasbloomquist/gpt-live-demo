/** Small status pills shared by the list and detail pages. */
import { OfflineIcon, RefreshIcon } from "@/components/icons";
import type { AnalysisStatus, OutcomeStatus } from "@/lib/types";

const OUTCOMES: Record<OutcomeStatus, { label: string; tone: string }> = {
  resolved: { label: "Resolved", tone: "pill-green" },
  partially_resolved: { label: "Partly resolved", tone: "pill-amber" },
  unresolved: { label: "Unresolved", tone: "pill-red" },
  not_applicable: { label: "No request", tone: "" },
};

export function OutcomePill({ status }: { status: OutcomeStatus | undefined }) {
  const o = status ? OUTCOMES[status] : undefined;
  if (!o) return null;
  return <span className={`pill ${o.tone}`}>{o.label}</span>;
}

/** Renders nothing for "done": a finished analysis speaks for itself. */
export function AnalysisStatusPill({ status }: { status: AnalysisStatus | null }) {
  switch (status) {
    case "done":
      return null;
    case "pending":
      return <span className="pill pill-blue">Queued</span>;
    case "running":
      return (
        <span className="pill pill-blue">
          <RefreshIcon className="spin" /> Analyzing
        </span>
      );
    case "failed":
      return <span className="pill pill-red">Analysis failed</span>;
    default:
      return <span className="pill">Not analyzed</span>;
  }
}

/** `compact` is the short form for list rows; the detail page spells it out. */
export function HeuristicBadge({ compact = false }: { compact?: boolean }) {
  return (
    <span className="pill" title="Scored by the analyzer's deterministic offline rules, not an LLM">
      <OfflineIcon /> {compact ? "Heuristic" : "Offline heuristic analysis"}
    </span>
  );
}

export function isInFlight(status: AnalysisStatus | null | undefined): boolean {
  return status === "pending" || status === "running";
}
