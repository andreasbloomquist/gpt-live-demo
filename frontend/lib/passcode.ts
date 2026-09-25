/**
 * Optional demo passcode gate.
 *
 * If DEMO_PASSCODE is unset, everything is open (the localhost default). If it is set,
 * the token route, the analyzer-backed pages, and every server action require an
 * unlock cookie. The cookie holds `<issued_at>.<HMAC-SHA256(key = passcode, label + issued_at)>`:
 * it proves the visitor typed the passcode at `issued_at` without storing the passcode
 * itself. The server rejects cookies older than 12 hours (so a copied cookie value stops
 * working even if a browser ignores maxAge), and changing DEMO_PASSCODE invalidates every
 * existing cookie. It is httpOnly + SameSite=Strict, so page scripts can't read it and
 * cross-site requests don't carry it.
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
/** Tolerated clock skew for an `issued_at` slightly in the future (multi-instance hosts). */
const MAX_SKEW_S = 60;

function passcode(): string | null {
  return process.env.DEMO_PASSCODE || null;
}

export function passcodeRequired(): boolean {
  return passcode() !== null;
}

const nowS = () => Math.floor(Date.now() / 1000);

function unlockToken(secret: string, issuedAt: number): string {
  const sig = createHmac("sha256", secret).update(`${LABEL}\n${issuedAt}`).digest("base64url");
  return `${issuedAt}.${sig}`;
}

/** Valid if the signature matches its own `issued_at` and that is within the last 12 hours. */
function isValidToken(value: string, secret: string): boolean {
  const match = /^(\d{1,12})\.[A-Za-z0-9_-]{1,64}$/.exec(value);
  if (!match) return false;
  const issuedAt = Number(match[1]);
  const age = nowS() - issuedAt;
  if (age > MAX_AGE_S || age < -MAX_SKEW_S) return false;
  return safeEqual(value, unlockToken(secret, issuedAt));
}

/** Constant-time string compare (hashing first makes the lengths equal). */
function safeEqual(a: string, b: string): boolean {
  const ha = createHash("sha256").update(a).digest();
  const hb = createHash("sha256").update(b).digest();
  return timingSafeEqual(ha, hb);
}

export async function isUnlocked(): Promise<boolean> {
  // Read the cookie even when no passcode is set: that marks every gated page as
  // request-time, so a page built without DEMO_PASSCODE isn't prerendered as "open"
  // and then served that way after DEMO_PASSCODE is set at runtime.
  const value = (await cookies()).get(COOKIE)?.value;
  const secret = passcode();
  if (secret === null) return true;
  return value !== undefined && isValidToken(value, secret);
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
  (await cookies()).set(COOKIE, unlockToken(secret, nowS()), {
    httpOnly: true,
    sameSite: "strict",
    // Browsers treat http://localhost as secure, so this also works for local `npm start`.
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: MAX_AGE_S,
  });
  return true;
}

/**
 * Only same-origin relative paths are valid post-unlock destinations (no open redirect).
 * Besides "//host" and "/\host", this rejects control characters and whitespace:
 * browsers strip tabs and newlines from URLs, so "/\t/evil.example" would become
 * "//evil.example" once it reached the Location header.
 */
export function safeReturnPath(next: unknown): string {
  if (
    typeof next !== "string" ||
    next.length > 2048 ||
    !/^\/(?![/\\])[\x21-\x7e]*$/.test(next) ||
    next.includes("\\")
  ) {
    return "/";
  }
  return next;
}
