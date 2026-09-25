"use client";

/**
 * While an analysis is queued or running, re-render the server page (router.refresh)
 * so the result appears without a manual reload.
 *
 * Polls back off exponentially (2.5 s, 5 s, 10 s, … capped at 30 s), pause while the tab
 * is hidden, and give up after 3 minutes of *visible* time, so a stuck job can't poll
 * forever and a background tab doesn't use up the budget. Polling stops on its own once
 * nothing is in flight: the server re-renders this with active=false.
 *
 * With `showStatus`, it also renders the status sentence for the page ("updates
 * automatically", then "refresh to check" after giving up).
 */
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

const FIRST_DELAY_MS = 2_500;
const MAX_DELAY_MS = 30_000;
const MAX_VISIBLE_MS = 3 * 60_000;

export function AnalysisPoller({ active, showStatus = false }: { active: boolean; showStatus?: boolean }) {
  const router = useRouter();
  const [gaveUp, setGaveUp] = useState(false);

  useEffect(() => {
    if (!active) return;
    let delay = FIRST_DELAY_MS;
    let visibleMs = 0;
    let visibleSince: number | null = document.hidden ? null : performance.now();
    let timer: ReturnType<typeof setTimeout> | undefined;

    const visibleTotal = () =>
      visibleMs + (visibleSince === null ? 0 : performance.now() - visibleSince);

    const tick = () => {
      if (visibleTotal() >= MAX_VISIBLE_MS) {
        setGaveUp(true);
        return;
      }
      if (document.hidden) return; // resumed by onVisibility, with no poll wasted meanwhile
      router.refresh();
      delay = Math.min(delay * 2, MAX_DELAY_MS);
      timer = setTimeout(tick, delay);
    };

    const onVisibility = () => {
      if (document.hidden) {
        if (visibleSince !== null) visibleMs += performance.now() - visibleSince;
        visibleSince = null;
        clearTimeout(timer);
      } else {
        visibleSince = performance.now();
        tick(); // catch up right away: the result may have landed while hidden
      }
    };

    timer = setTimeout(tick, delay);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      setGaveUp(false); // a later in-flight analysis (e.g. re-analyze) starts fresh
    };
  }, [active, router]);

  if (!showStatus || !active) return null;
  return <>{gaveUp ? "Still analyzing. Refresh to check again." : "This page updates automatically."}</>;
}
