"use client";

/**
 * While an analysis is queued or running, re-render the server page every few
 * seconds so the result appears without a manual reload. Stops once nothing is in
 * flight (the server re-renders with active=false) or after a time cap, so a stuck
 * job can't poll forever.
 */
import { useEffect } from "react";
import { useRouter } from "next/navigation";

const INTERVAL_MS = 2_500;
const MAX_POLL_MS = 3 * 60_000;

export function AnalysisPoller({ active }: { active: boolean }) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const started = Date.now();
    const id = setInterval(() => {
      if (Date.now() - started > MAX_POLL_MS) clearInterval(id);
      else if (!document.hidden) router.refresh(); // don't poll from a background tab
    }, INTERVAL_MS);
    return () => clearInterval(id);
  }, [active, router]);
  return null;
}
