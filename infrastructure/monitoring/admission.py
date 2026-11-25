"""Compatibility entrypoint; web, workers and tests share one admission policy."""

from app.admission import evaluate

__all__ = ["evaluate"]
