"use client";

/**
 * Renders a timestamp in the viewer's own time zone.
 *
 * Server Components run in the server's zone (often UTC), so formatting there would
 * show the wrong local time. The server/hydration pass renders an explicit UTC time;
 * right after hydration, the client re-renders in local time. useSyncExternalStore is
 * just the idiomatic "am I hydrated?" check (no subscription needed).
 */
import { useSyncExternalStore } from "react";

const noopSubscribe = () => () => {};

const OPTIONS: Record<"datetime" | "date" | "time", Intl.DateTimeFormatOptions> = {
  datetime: { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" },
  date: { weekday: "long", month: "long", day: "numeric", year: "numeric" },
  time: { hour: "numeric", minute: "2-digit", second: "2-digit" },
};

export function LocalTime({ iso, format = "datetime" }: { iso: string; format?: keyof typeof OPTIONS }) {
  const hydrated = useSyncExternalStore(noopSubscribe, () => true, () => false);
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return <span>Unknown time</span>;

  const text = hydrated
    ? date.toLocaleString(undefined, OPTIONS[format])
    : `${date.toLocaleString("en-US", { ...OPTIONS[format], timeZone: "UTC" })}${format === "date" ? "" : " UTC"}`;
  return (
    <time dateTime={date.toISOString()}>
      {text}
    </time>
  );
}
