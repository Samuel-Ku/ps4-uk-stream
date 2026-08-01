from __future__ import annotations

from . import register
from .uakino import UakinoProvider
from .ufdub import UFDubProvider


def bootstrap() -> None:
    register(UakinoProvider())
    register(UFDubProvider())


bootstrap()
