"""LLM-as-judge with structured output, shared by both tiers.

One grader for both tiers means brain and voice pass rates are comparable: a case that passes
in brain but fails in voice points at the voice layer, not at a different grader.

Judge design choices:

* **Rubric in, binary verdict out** (plus a reason). Scales invite drift; pass/fail against
  explicit criteria is easier to calibrate and to read in a PR comment.
* **Structured output** (``responses.parse`` with a pydantic schema): no regex-parsing a
  verdict out of prose, and the reason comes *before* the verdict so the model reasons first.
* **Transcript only.** The judge never sees the agent's prompts, so it grades behaviour, not
  intent, and prompt edits cannot leak into grading.
* **Separate, pinned judge model** (``EVALS_JUDGE_MODEL``) so upgrading the agent's model does
  not silently change the grader.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from evals.runners.common import JudgeVerdict, text_cost

JUDGE_MODEL = os.environ.get("EVALS_JUDGE_MODEL", "gpt-5.6-luna")

JUDGE_INSTRUCTIONS = """\
You grade transcripts of a phone-based voice concierge agent against a rubric.

Rules:
- Grade ONLY what is in the transcript. Assistant lines were spoken aloud; tool lines show what
  the agent's backend called and received.
- The rubric lists pass criteria. PASS only if every criterion is met. Anything the rubric
  marks as a FAIL condition is decisive.
- Do not reward or penalise style the rubric does not mention.
- If the transcript is empty or cut off before the agent could satisfy the rubric, FAIL.
Explain briefly, then give the verdict."""


class _Verdict(BaseModel):
    reasoning: str = Field(description="Two or three sentences citing the transcript.")
    verdict: bool = Field(description="true = PASS, false = FAIL")


async def judge_transcript(
    client: Any, *, rubric: str, transcript: str, model: str = JUDGE_MODEL
) -> tuple[JudgeVerdict, float]:
    """Grade ``transcript`` with ``rubric``. Returns (verdict, cost_usd)."""
    response = await client.responses.parse(
        model=model,
        instructions=JUDGE_INSTRUCTIONS,
        input=f"<rubric>\n{rubric}\n</rubric>\n\n<transcript>\n{transcript}\n</transcript>",
        text_format=_Verdict,
        reasoning={"effort": "low"},
    )
    parsed: _Verdict | None = response.output_parsed
    usage = getattr(response, "usage", None)
    cost = text_cost(model, usage.input_tokens, usage.output_tokens) if usage is not None else 0.0
    if parsed is None:
        return JudgeVerdict(False, "judge returned no parseable verdict", model), cost
    return JudgeVerdict(parsed.verdict, parsed.reasoning, model), cost
