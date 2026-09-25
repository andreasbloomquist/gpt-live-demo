/** Issues the analyzer flagged, each linked to its turn when it has one. */
import { FlagIcon } from "@/components/icons";
import { humanize } from "@/lib/format";
import type { Flag, FlagType } from "@/lib/types";
import { turnAnchor } from "./CallTranscript";
import styles from "./detail.module.css";

const LABELS: Record<FlagType, string> = {
  escalation_needed: "Escalation needed",
  hallucination_risk: "Hallucination risk",
  policy_violation: "Policy violation",
  tool_failure: "Tool failure",
  caller_repeated: "Caller repeated",
  long_silence: "Long silence",
  other: "Other",
};

export function Flags({ flags }: { flags: Flag[] }) {
  return (
    <ul className={`card ${styles.flags}`}>
      {flags.map((f, i) => (
        <li key={i}>
          <FlagIcon className={styles.flagIcon} />
          <div>
            {/* The analyzer may add flag types this app doesn't know yet. */}
            <p className={styles.flagType}>{LABELS[f.type] ?? humanize(f.type)}</p>
            <p className={styles.flagDetail}>{f.detail}</p>
          </div>
          {f.turn_id && (
            <a className={styles.flagLink} href={`#${turnAnchor(f.turn_id)}`}>
              View turn
            </a>
          )}
        </li>
      ))}
    </ul>
  );
}
