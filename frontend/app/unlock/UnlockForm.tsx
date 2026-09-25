"use client";

import { useActionState } from "react";
import { unlock } from "@/app/actions";
import { LockIcon } from "@/components/icons";
import styles from "./unlock.module.css";

export function UnlockForm({ next }: { next: string }) {
  const [state, action, pending] = useActionState(unlock, { error: null });
  return (
    <main className={`page ${styles.wrap}`}>
      <form action={action} className={`card ${styles.card}`}>
        <div className={styles.icon}>
          <LockIcon />
        </div>
        <h1>This demo is private</h1>
        <p className="muted">Enter the passcode you were given to continue.</p>
        <input type="hidden" name="next" value={next} />
        <label htmlFor="passcode" className="sr-only">
          Passcode
        </label>
        <input
          id="passcode"
          name="passcode"
          type="password"
          autoComplete="current-password"
          placeholder="Passcode"
          required
          maxLength={256}
          autoFocus
          className={styles.input}
          aria-invalid={state.error ? true : undefined}
          aria-describedby={state.error ? "passcode-error" : undefined}
        />
        {state.error && (
          <p id="passcode-error" className={styles.error} role="alert">
            {state.error}
          </p>
        )}
        <button className="btn btn-primary" disabled={pending}>
          {pending ? "Checking…" : "Continue"}
        </button>
      </form>
    </main>
  );
}
