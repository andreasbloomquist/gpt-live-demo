"use client";

/** Translucent top navigation. Client-side only to mark the active link. */
import Link from "next/link";
import { usePathname } from "next/navigation";
import styles from "./Nav.module.css";

const LINKS = [
  { href: "/", label: "Live" },
  { href: "/calls", label: "Calls" },
];

export function Nav() {
  const pathname = usePathname();
  const isActive = (href: string) => (href === "/" ? pathname === "/" : pathname.startsWith(href));

  return (
    <header className={styles.nav}>
      <nav className={styles.inner} aria-label="Main">
        <Link href="/" className={styles.brand}>
          <span className={styles.mark} aria-hidden="true" />
          GPT-Live Concierge
        </Link>
        <ul className={styles.links}>
          {LINKS.map(({ href, label }) => (
            <li key={href}>
              <Link
                href={href}
                className={styles.link}
                aria-current={isActive(href) ? "page" : undefined}
              >
                {label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </header>
  );
}
