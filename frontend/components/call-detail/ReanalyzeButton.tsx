"use client";

/** Triggers a fresh analysis via a Server Action; the page then polls until it's done. */
import { useState, useTransition } from "react";
import { reanalyze } from "@/app/actions";
import { RefreshIcon } from "@/components/icons";
import styles from "./detail.module.css";

type Props = {
  callId: string;
  /** True while an analysis is already queued or running (disables the button). */
  busy: boolean;
  label?: string;
};

export function ReanalyzeButton({ callId, busy, label = "Re-analyze" }: Props) {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const working = pending || busy;

  return (
    <div className={styles.reanalyze}>
      <button
        className="btn"
        disabled={working}
        onClick={() =>
          startTransition(async () => {
            const res = await reanalyze(callId);
            setError(res.error);
          })
        }
      >
        <RefreshIcon className={working ? "spin" : undefined} />
        {working ? "Analyzing…" : label}
      </button>
      {error && (
        <p className={styles.actionError} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
