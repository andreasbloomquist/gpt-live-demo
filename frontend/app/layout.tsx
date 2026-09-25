import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "GPT-Live Voice Agent",
  description: "Talk to an OpenAI GPT-Live voice agent running on LiveKit Agents.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
