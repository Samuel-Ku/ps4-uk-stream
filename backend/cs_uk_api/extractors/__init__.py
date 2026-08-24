"""Extractor layer (issue #8)."""
from __future__ import annotations

from .base import BaseExtractor, ExtractResult
from .playlist import walk_playlist
from .regex import RegexExtractor

__all__ = [
    "BaseExtractor",
    "ExtractResult",
    "RegexExtractor",
    "walk_playlist",
]