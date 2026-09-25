"""Assessment providers and the factory that picks one from settings."""

from __future__ import annotations

from ..config import Settings
from .base import AnalysisProvider, ProviderError
from .heuristic import HeuristicProvider
from .openai_provider import OpenAIProvider

__all__ = ["AnalysisProvider", "HeuristicProvider", "OpenAIProvider", "ProviderError", "build_provider"]


def build_provider(settings: Settings) -> AnalysisProvider:
    """Instantiate the configured provider, failing fast if it can't work."""
    choice = settings.resolved_provider()
    if choice == "heuristic":
        return HeuristicProvider()
    return OpenAIProvider.from_settings(settings)
