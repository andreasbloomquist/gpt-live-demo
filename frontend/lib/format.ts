/** Small formatting helpers shared by server and client components. */

/** 75 -> "1:15"; 3725 -> "1:02:05". */
export function formatClock(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

/** 75 -> "1m 15s"; 42 -> "42s". */
export function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const m = Math.floor(s / 60);
  if (m === 0) return `${s}s`;
  return s % 60 === 0 ? `${m}m` : `${m}m ${s % 60}s`;
}

export function formatPercent(fraction: number): string {
  return `${Math.round(fraction * 100)}%`;
}

/** "customer_satisfaction" -> "Customer satisfaction". */
export function humanize(snake: string): string {
  const s = snake.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}
