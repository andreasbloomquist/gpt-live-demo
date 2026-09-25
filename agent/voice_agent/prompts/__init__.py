"""Modular prompt system: compose GPT-Live voice + backend instructions from data files."""

from .composer import (
    DEFAULT_PROMPTS_DIR,
    PromptBundle,
    PromptComposer,
    PromptCompositionError,
    PromptModule,
    normalize_prompt_text,
)

__all__ = [
    "DEFAULT_PROMPTS_DIR",
    "PromptBundle",
    "PromptComposer",
    "PromptCompositionError",
    "PromptModule",
    "normalize_prompt_text",
]
