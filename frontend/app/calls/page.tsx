/**
 * /calls — call history, server-rendered from the Call Analyzer.
 * The first page is fetched here; <CallList> loads further pages via a Server Action.
 */
import type { Metadata } from "next";
import { AnalysisPoller } from "@/components/calls/AnalysisPoller";
import { isInFlight } from "@/components/calls/Badges";
import { CallList } from "@/components/calls/CallList";
import { AnalyzerUnavailable, CallsHeader, EmptyCalls } from "@/components/calls/States";
import { analyzerErrorMessage, listCalls } from "@/lib/analyzer";
import { requireUnlocked } from "@/lib/passcode";
import type { CallPage } from "@/lib/types";

export const metadata: Metadata = { title: "Calls" };

export default async function CallsPage() {
  await requireUnlocked("/calls");

  let page: CallPage | null = null;
  let error: string | null = null;
  try {
    page = await listCalls();
  } catch (e) {
    error = analyzerErrorMessage(e);
  }

  return (
    <main className="page">
      <CallsHeader />
      {error !== null ? (
        <AnalyzerUnavailable message={error} retryHref="/calls" />
      ) : page && page.items.length > 0 ? (
        <>
          <CallList initial={page} />
          <AnalysisPoller active={page.items.some((c) => isInFlight(c.status))} />
        </>
      ) : (
        <EmptyCalls />
      )}
    </main>
  );
}
