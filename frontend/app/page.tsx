import { LiveExperience } from "@/components/live/LiveExperience";
import { requireUnlocked } from "@/lib/passcode";

export default async function LivePage() {
  await requireUnlocked("/");
  return <LiveExperience />;
}
