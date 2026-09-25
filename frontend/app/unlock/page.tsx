/** Passcode screen, shown only when DEMO_PASSCODE is set (see lib/passcode.ts). */
import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { passcodeRequired, safeReturnPath } from "@/lib/passcode";
import { UnlockForm } from "./UnlockForm";

export const metadata: Metadata = { title: "Enter passcode" };

export default async function UnlockPage({
  searchParams,
}: {
  searchParams: Promise<{ next?: string | string[] }>;
}) {
  const { next } = await searchParams;
  const returnTo = safeReturnPath(next);
  if (!passcodeRequired()) redirect(returnTo);
  return <UnlockForm next={returnTo} />;
}
