"""Controlled model harness."""

from aibb.harness.context import ContextEnvelope, build_context_envelope
from aibb.harness.engine import AibbHarnessEngine, EngineSnapshot
from aibb.harness.openrouter import OpenRouterAdapter, openrouter_model

__all__ = [
    "AibbHarnessEngine",
    "ContextEnvelope",
    "EngineSnapshot",
    "OpenRouterAdapter",
    "build_context_envelope",
    "openrouter_model",
]
