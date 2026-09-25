"""Evaluation harness for the GPT-Live voice agent.

Kept deliberately import-light: the change-impact probe imports :mod:`evals.toolschema` while
running against an *older* checkout of ``voice_agent``, so nothing here may import the agent,
LiveKit, or OpenAI at module import time.
"""
