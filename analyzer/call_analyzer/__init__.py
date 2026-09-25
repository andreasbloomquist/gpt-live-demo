"""Call Analyzer: stores voice-agent call records and grades them against a versioned rubric.

Entry points: :func:`call_analyzer.api.create_app` (HTTP service) and ``python -m call_analyzer``
(CLI). The wire contracts live in :mod:`call_analyzer.models`.
"""

__version__ = "0.1.0"
