from __future__ import annotations

from . import register
from .uakino import UakinoProvider


def bootstrap() -> None:
    register(UakinoProvider())


bootstrap()
