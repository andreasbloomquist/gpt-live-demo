/**
 * /calls/[id] — one call: header, scorecard, flags, metrics, sentiment, and the full
 * transcript. Server-rendered; polls (router.refresh) while the analysis is in flight.
 */
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { CallTranscript } from "@/components/call-detail/CallTranscript";
import { Flags } from "@/components/call-detail/Flags";
import { MetricsRow } from "@/components/call-detail/MetricsRow";
import { ReanalyzeButton } from "@/components/call-detail/ReanalyzeButton";
import { Scorecard } from "@/components/call-detail/Scorecard";
import { Sparkline } from "@/components/call-detail/Sparkline";
import { AnalysisPoller } from "@/components/calls/AnalysisPoller";
import {
  AnalysisStatusPill,
  HeuristicBadge,
  isInFlight,
  OutcomePill,
} from "@/components/calls/Badges";
import { ScoreRing } from "@/components/calls/ScoreRing";
import { AnalyzerUnavailable } from "@/components/calls/States";
import { ChevronLeftIcon } from "@/components/icons";
import { LocalTime } from "@/components/LocalTime";
import { AnalyzerError, analyzerErrorMessage, getCall, isValidCallId } from "@/lib/analyzer";
import { formatDuration } from "@/lib/format";
import { requireUnlocked } from "@/lib/passcode";
import type { Analysis, CallDetail } from "@/lib/types";
import styles from "@/components/call-detail/detail.module.css";

export const metadata: Metadata = { title: "Call" };

export default async function CallPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  await requireUnlocked(`/calls/${encodeURIComponent(id)}`);
  // Reject anything outside the analyzer's call-id alphabet before it gets near a URL.
  if (!isValidCallId(id)) notFound();

  let detail: CallDetail;
  try {
    detail = await getCall(id);
  } catch (e) {
    if (e instanceof AnalyzerError && e.kind === "not_found") notFound();
    return (
      <main className="page">
        <BackLink />
        <AnalyzerUnavailable
          headingLevel={1}
          title="Can’t load this call right now"
          message={analyzerErrorMessage(e)}
          retryHref={`/calls/${encodeURIComponent(id)}`}
        />
      </main>
    );
  }

  const { record, analysis } = detail;
  const done = analysis?.status === "done" ? analysis : null;
  const inFlight = isInFlight(analysis?.status);

  return (
    <main className="page">
      <BackLink />

      <header className={styles.hero}>
        <div className={styles.heroText}>
          <p className="eyebrow">
            <LocalTime iso={record.started_at} format="date" />
          </p>
          <h1>{done?.caller_intent || "Call with Ava"}</h1>
          {done?.summary && <p className={styles.lede}>{done.summary}</p>}
          <div className={styles.facts}>
            <OutcomePill status={done?.outcome?.status} />
            <AnalysisStatusPill status={analysis?.status ?? null} />
            <span className="pill">{formatDuration(record.duration_s)}</span>
            <span className="pill">{record.turns.length} turns</span>
            {record.tool_calls.length > 0 && (
              <span className="pill">
                {record.tool_calls.length} tool call{record.tool_calls.length === 1 ? "" : "s"}
              </span>
            )}
          </div>
        </div>
        {done && (
          <div className={styles.heroScore}>
            <ScoreRing score={done.overall_score} size={112} />
            <p>Overall</p>
          </div>
        )}
      </header>

      <AnalysisBar callId={record.call_id} analysis={analysis} inFlight={inFlight} />

      {done ? (
        <>
          {done.outcome?.reason && (
            <section className={styles.section} aria-labelledby="outcome">
              <h2 id="outcome">Outcome</h2>
              <p className={`card ${styles.outcome}`}>{done.outcome.reason}</p>
            </section>
          )}

          <section className={styles.section} aria-labelledby="scorecard">
            <h2 id="scorecard">Scorecard</h2>
            <p className={styles.sectionLede}>
              Each dimension is scored 1–5 against the rubric. Quotes link to the moment in the
              transcript.
            </p>
            <Scorecard scores={done.scores} />
          </section>

          {done.flags.length > 0 && (
            <section className={styles.section} aria-labelledby="flags">
              <h2 id="flags">Flags</h2>
              <Flags flags={done.flags} />
            </section>
          )}

          {done.metrics && (
            <section className={styles.section} aria-labelledby="metrics">
              <h2 id="metrics">Metrics</h2>
              <p className={styles.sectionLede}>
                Computed exactly from the call record, not by a model.
              </p>
              <MetricsRow metrics={done.metrics} />
            </section>
          )}

          <section className={styles.section} aria-labelledby="sentiment">
            <h2 id="sentiment">Caller sentiment</h2>
            <div className={`card ${styles.sparkCard}`}>
              <Sparkline points={done.sentiment} />
            </div>
          </section>
        </>
      ) : null}

      <section className={styles.section} aria-labelledby="transcript">
        <h2 id="transcript">Transcript</h2>
        <CallTranscript record={record} />
      </section>

      <footer className={styles.provenance}>
        <span>
          Call <code>{record.call_id}</code>
        </span>
        {record.models?.voice_model && (
          <span>
            Voice <code>{record.models.voice_model}</code>
          </span>
        )}
        {record.models?.backend_model && (
          <span>
            Backend <code>{record.models.backend_model}</code>
          </span>
        )}
        {record.prompt?.version && (
          <span>
            Prompt <code>{record.prompt.version}</code>
          </span>
        )}
      </footer>
    </main>
  );
}

function BackLink() {
  return (
    <Link href="/calls" className={styles.back}>
      <ChevronLeftIcon /> Calls
    </Link>
  );
}

/** Who analyzed the call (provider/model/rubric), its status, and the Re-analyze action. */
function AnalysisBar({
  callId,
  analysis,
  inFlight,
}: {
  callId: string;
  analysis: Analysis | null;
  inFlight: boolean;
}) {
  const info = analysis?.analyzer;
  return (
    <div className={`card ${styles.analysisBar}`}>
      <div className={styles.analysisInfo}>
        {analysis === null ? (
          <p>This call hasn&rsquo;t been analyzed yet.</p>
        ) : inFlight ? (
          <p role="status">
            {analysis.status === "pending" ? "Queued for analysis." : "Analyzing this call…"}{" "}
            <AnalysisPoller active showStatus />
          </p>
        ) : analysis.status === "failed" ? (
          <p role="alert">
            Analysis failed{analysis.error ? `: ${analysis.error}` : "."} Try running it again.
          </p>
        ) : (
          <>
            {info?.provider === "heuristic" && <HeuristicBadge />}
            <p className="muted">
              Analyzed by <strong>{info?.provider ?? "unknown"}</strong>
              {info?.model && (
                <>
                  {" "}
                  · <code>{info.model}</code>
                </>
              )}{" "}
              · rubric v{info?.rubric_version ?? "?"}
              {analysis.created_at && (
                <>
                  {" "}
                  · <LocalTime iso={analysis.created_at} />
                </>
              )}
            </p>
          </>
        )}
      </div>
      <ReanalyzeButton
        callId={callId}
        busy={inFlight}
        label={analysis === null ? "Analyze" : "Re-analyze"}
      />
    </div>
  );
}
