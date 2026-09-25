/**
 * Display metadata for the analyzer's scoring dimensions. Labels and questions mirror
 * analyzer/call_analyzer/rubric.yaml; order is the rubric's (heaviest weight first).
 * Keep in sync when the rubric changes (unknown dimensions still render, humanized).
 */
import type { Dimension } from "@/lib/types";

type DimensionMeta = { label: string; question: string; inverted?: boolean };

export const DIMENSION_META: Record<Dimension, DimensionMeta> = {
  resolution: {
    label: "Resolution",
    question: "Did the caller leave with what they called for, or a clear next step?",
  },
  customer_satisfaction: {
    label: "Customer satisfaction",
    question: "How satisfied does the caller seem by the end of the call?",
  },
  agent_helpfulness: {
    label: "Agent helpfulness",
    question: "Did Ava proactively move the caller toward their goal?",
  },
  accuracy_groundedness: {
    label: "Accuracy & groundedness",
    question: "Are Ava's statements supported by tool results or common knowledge?",
  },
  policy_adherence: {
    label: "Policy adherence",
    question: "Did Ava follow the concierge policy?",
  },
  conversation_flow: {
    label: "Conversation flow",
    question: "Natural turn-taking: no talking over, dead air, or loops?",
  },
  efficiency: {
    label: "Efficiency",
    question: "As short as possible while complete, with voice-sized answers?",
  },
  tone_empathy: {
    label: "Tone & empathy",
    question: "Warm and professional, acknowledging frustration when present?",
  },
  customer_frustration: {
    label: "Customer frustration",
    question: "How frustrated did the caller become at any point?",
    inverted: true,
  },
};

export const DIMENSION_ORDER = Object.keys(DIMENSION_META) as Dimension[];

const FRUSTRATION_LEVELS = ["None", "Mild", "Noticeable", "High", "Severe"];

/**
 * Maps a 1–5 score to how *good* it is (1–5). Every dimension is "higher is better"
 * except customer_frustration, where 1 = no frustration = best.
 */
export function goodness(dimension: string, score: number): number {
  return DIMENSION_META[dimension as Dimension]?.inverted ? 6 - score : score;
}

export function frustrationLevel(score: number): string {
  return FRUSTRATION_LEVELS[Math.min(5, Math.max(1, Math.round(score))) - 1];
}
