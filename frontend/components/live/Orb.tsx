/**
 * The agent "orb": a soft gradient sphere whose motion follows the agent state
 * (breathing while listening, swirling while thinking, glowing while speaking).
 * Purely decorative; state is announced separately via a role="status" label.
 * All motion is CSS and switches off under prefers-reduced-motion.
 */
import styles from "./Orb.module.css";

type Props = {
  state: string;
  size?: "md" | "lg";
  children?: React.ReactNode;
};

export function Orb({ state, size = "lg", children }: Props) {
  return (
    <div className={`${styles.orb} ${styles[size]}`} data-state={state} aria-hidden="true">
      <div className={styles.glow} />
      <div className={styles.sphere}>
        <div className={styles.swirl} />
        <div className={styles.sheen} />
        {children && <div className={styles.content}>{children}</div>}
      </div>
    </div>
  );
}
