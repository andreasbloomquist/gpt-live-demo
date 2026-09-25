"use server";

/**
 * Server Actions: the only way the browser can make the server talk to the analyzer.
 * Each one is a public POST endpoint, so each re-checks the passcode gate and
 * validates its arguments before doing anything.
 */
import { refresh } from "next/cache";
import { redirect } from "next/navigation";
import {
  analyzerErrorMessage,
  isValidCallId,
  isValidCursor,
  listCalls,
  reanalyzeCall,
} from "@/lib/analyzer";
import { isUnlocked, safeReturnPath, tryUnlock } from "@/lib/passcode";
import type { CallPage } from "@/lib/types";

const LOCKED_MESSAGE = "Enter the demo passcode first.";
/** Matches the input's maxLength in app/unlock/UnlockForm.tsx. */
const MAX_PASSCODE_LENGTH = 256;
const WRONG_PASSCODE_DELAY_MS = 500;

export type ActionResult = { error: string | null };
export type LoadMoreResult = { page: CallPage; error: null } | { page: null; error: string };

export async function unlock(_prev: ActionResult, form: FormData): Promise<ActionResult> {
  const input = form.get("passcode");
  if (typeof input !== "string" || input.length === 0 || input.length > MAX_PASSCODE_LENGTH) {
    return { error: "Enter the passcode." };
  }
  if (!(await tryUnlock(input))) {
    // A fixed delay only slows a sequential guesser: parallel requests are not limited at all.
    // The real brute-force defence is a rate limit at the edge (see frontend/README.md).
    await new Promise((resolve) => setTimeout(resolve, WRONG_PASSCODE_DELAY_MS));
    return { error: "That passcode isn't right." };
  }
  redirect(safeReturnPath(form.get("next")));
}

export async function loadMoreCalls(cursor: string): Promise<LoadMoreResult> {
  if (!(await isUnlocked())) return { page: null, error: LOCKED_MESSAGE };
  if (!isValidCursor(cursor)) return { page: null, error: "Invalid page cursor." };
  try {
    return { page: await listCalls({ cursor }), error: null };
  } catch (e) {
    return { page: null, error: analyzerErrorMessage(e) };
  }
}

export async function reanalyze(callId: string): Promise<ActionResult> {
  if (!(await isUnlocked())) return { error: LOCKED_MESSAGE };
  if (!isValidCallId(callId)) return { error: "Invalid call id." };
  try {
    await reanalyzeCall(callId);
  } catch (e) {
    return { error: analyzerErrorMessage(e) };
  }
  refresh(); // re-render the page so it picks up the new "pending" status and starts polling
  return { error: null };
}
