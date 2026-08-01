"""Extractor layer (issue #8)."""
from __future__ import annotations

from .base import BaseExtractor, ExtractResult
from .iframe import IframeExtractor
from .playerjson import PlayerJsonExtractor
from .regex import RegexExtractor

__all__ = [
    "BaseExtractor",
    "ExtractResult",
    "IframeExtractor",
    "PlayerJsonExtractor",
    "RegexExtractor",
]