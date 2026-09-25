"""Web search via OpenAI's hosted ``web_search`` tool.

This is a *provider tool*: it is executed server-side by OpenAI on the backend Responses
model, so there's no code of ours in the loop, no search API key to manage, and results never
round-trip through the agent process. The trade-off is that it only works where the backend is
an OpenAI Responses model, which is exactly GPT-Live's ``delegation="responses"`` setup.
"""

from __future__ import annotations

from livekit.plugins.openai.tools import WebSearch

from ..config import Settings


def build_web_search_tool(settings: Settings) -> WebSearch:
    """``search_context_size="low"`` by default: cheaper and faster, enough for spoken answers."""
    return WebSearch(search_context_size=settings.web_search_context_size)
