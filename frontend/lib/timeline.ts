/**
 * Interleaves a call's turns and tool calls into one chronological transcript.
 *
 * Turns keep their recorded order (full-duplex speech overlaps, so the analyzer's order
 * is the source of truth). Each timed tool call is placed before the first turn that
 * started after it; tool calls without a timestamp go at the end.
 */
import type { ToolCall, Turn } from "@/lib/types";

export type TimelineItem = { kind: "turn"; turn: Turn } | { kind: "tool"; call: ToolCall };

const ms = (iso: string | null): number | null => {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
};

export function buildTimeline(turns: Turn[], toolCalls: ToolCall[]): TimelineItem[] {
  const pending = toolCalls
    .map((call) => ({ call, at: ms(call.created_at) }))
    .sort((a, b) => (a.at ?? Infinity) - (b.at ?? Infinity));

  const items: TimelineItem[] = [];
  let next = 0;
  for (const turn of turns) {
    const start = ms(turn.started_at);
    while (start !== null && next < pending.length) {
      const at = pending[next].at;
      if (at === null || at >= start) break;
      items.push({ kind: "tool", call: pending[next++].call });
    }
    items.push({ kind: "turn", turn });
  }
  for (; next < pending.length; next++) items.push({ kind: "tool", call: pending[next].call });
  return items;
}

/** Seconds from the call start to `iso`, or null if either timestamp is missing. */
export function offsetFrom(callStart: string, iso: string | null): number | null {
  const a = ms(callStart);
  const b = ms(iso);
  return a === null || b === null ? null : Math.max(0, (b - a) / 1000);
}
