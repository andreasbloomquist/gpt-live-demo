/**
 * Optional demo passcode gate.
 *
 * If DEMO_PASSCODE is unset, everything is open (the localhost default). If it is set,
 * the token route, the analyzer-backed pages, and every server action require an
 * unlock cookie. The cookie holds HMAC-SHA256(key = passcode, fixed label): it proves
 * the visitor once typed the passcode without storing the passcode itself, and changing
 * DEMO_PASSCODE invalidates every existing cookie. It is httpOnly + SameSite=Strict, so
 * page scripts can't read it and cross-site requests don't carry it.
 *
 * This is a speed bump for a public demo URL, not user auth: there are no accounts, and
 * anyone with the passcode gets in. Pair it with a rate limit at your host/proxy.
 */
import "server-only";
import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import { cookies } from "next/headers";
import { redirect } from "next/navigation";

const COOKIE = "demo_unlock";
const LABEL = "gpt-live-demo/unlock/v1";
const MAX_AGE_S = 60 * 60 * 12;

function passcode(): string | null {
  return process.env.DEMO_PASSCODE || null;
}

export function passcodeRequired(): boolean {
  return passcode() !== null;
}

function unlockToken(secret: string): string {
  return createHmac("sha256", secret).update(LABEL).digest("base64url");
}

/** Constant-time string compare (hashing first makes the lengths equal). */
function safeEqual(a: string, b: string): boolean {
  const ha = createHash("sha256").update(a).digest();
  const hb = createHash("sha256").update(b).digest();
  return timingSafeEqual(ha, hb);
}

export async function isUnlocked(): Promise<boolean> {
  const secret = passcode();
  if (secret === null) return true;
  const value = (await cookies()).get(COOKIE)?.value;
  return value !== undefined && safeEqual(value, unlockToken(secret));
}

/** For pages: send locked visitors to /unlock, then back here. */
export async function requireUnlocked(returnTo: string): Promise<void> {
  if (!(await isUnlocked())) redirect(`/unlock?next=${encodeURIComponent(returnTo)}`);
}

/** Checks a submitted passcode and, if right, sets the unlock cookie. */
export async function tryUnlock(input: string): Promise<boolean> {
  const secret = passcode();
  if (secret === null) return true;
  if (!safeEqual(input, secret)) return false;
  (await cookies()).set(COOKIE, unlockToken(secret), {
    httpOnly: true,
    sameSite: "strict",
    // Browsers treat http://localhost as secure, so this also works for local `npm start`.
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: MAX_AGE_S,
  });
  return true;
}

/** Only same-origin relative paths are valid post-unlock destinations (no open redirect). */
export function safeReturnPath(next: unknown): string {
  if (typeof next !== "string" || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) {
    return "/";
  }
  return next;
}
