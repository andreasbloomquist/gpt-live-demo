"use client";

/**
 * The call list after the first page. The first page is server-rendered and passed in;
 * "Load more" asks a Server Action for the next page (the analyzer token never reaches
 * the browser) and appends it.
 */
import { useState, useTransition } from "react";
import { loadMoreCalls } from "@/app/actions";
import { AlertIcon } from "@/components/icons";
import type { CallPage, CallSummary } from "@/lib/types";
import { CallCard } from "./CallCard";
import styles from "./calls.module.css";

export function CallList({ initial }: { initial: CallPage }) {
  const [more, setMore] = useState<CallSummary[]>([]);
  const [cursor, setCursor] = useState(initial.next_cursor);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  // If a server refresh (polling) changes the first page, keep it authoritative and
  // drop appended rows it now contains.
  const seen = new Set(initial.items.map((c) => c.call_id));
  const rows = [...initial.items, ...more.filter((c) => !seen.has(c.call_id))];

  const loadMore = () => {
    if (!cursor) return;
    startTransition(async () => {
      const res = await loadMoreCalls(cursor);
      if (res.error !== null) {
        setError(res.error);
        return;
      }
      setError(null);
      setMore((prev) => [...prev, ...res.page.items]);
      setCursor(res.page.next_cursor);
    });
  };

  return (
    <>
      <ul className={styles.list}>
        {rows.map((call) => (
          <CallCard key={call.call_id} call={call} />
        ))}
      </ul>
      {error && (
        <p className={`notice ${styles.loadError}`} role="alert">
          <AlertIcon />
          <span>{error}</span>
        </p>
      )}
      {cursor && (
        <div className={styles.more}>
          <button className="btn" onClick={loadMore} disabled={pending}>
            {pending ? "Loading…" : "Load more"}
          </button>
        </div>
      )}
    </>
  );
}
