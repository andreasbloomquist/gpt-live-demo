import type { Metadata, Viewport } from "next";
import { Nav } from "@/components/Nav";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "GPT-Live Concierge", template: "%s · GPT-Live Concierge" },
  description:
    "Talk to a GPT-Live voice concierge running on LiveKit Agents, then review every call's transcript and analysis.",
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f5f5f7" },
    { media: "(prefers-color-scheme: dark)", color: "#000000" },
  ],
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Nav />
        {children}
        <footer className="site-footer">
          GPT-Live × LiveKit Agents reference demo ·{" "}
          <a href="https://github.com/andreasbloomquist/gpt-live-demo" target="_blank" rel="noreferrer">
            Source on GitHub
          </a>
        </footer>
      </body>
    </html>
  );
}
