"""Jellyfin facade package (spec #100). Exports the router for ``main.py``."""

from .router import router

__all__ = ["router"]
