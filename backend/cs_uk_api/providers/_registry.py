from __future__ import annotations

from . import register
from .animeua import AnimeUAProvider
from .bambooua import BambooUAProvider
from .cikavaideya import CikavaIdeyaProvider
from .hentaiukr import HentaiUkrProvider
from .kinotron import KinoTronProvider
from .kinovezha import KinoVezhaProvider
from .uaflix import UAFlixProvider
from .uakino import UakinoProvider
from .ufdub import UFDubProvider
from .unimay import UnimayProvider


def bootstrap() -> None:
    register(UakinoProvider())
    register(UFDubProvider())
    register(UnimayProvider())
    register(KinoTronProvider())
    register(CikavaIdeyaProvider())
    register(HentaiUkrProvider())
    register(BambooUAProvider())
    register(KinoVezhaProvider())
    register(AnimeUAProvider())
    register(UAFlixProvider())


bootstrap()
