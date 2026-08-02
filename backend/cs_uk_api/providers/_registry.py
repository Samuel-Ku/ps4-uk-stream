from __future__ import annotations

from . import register
from .animeon import AnimeONProvider
from .animeua import AnimeUAProvider
from .anitubeinua import AnitubeinuaProvider
from .bambooua import BambooUAProvider
from .cikavaideya import CikavaIdeyaProvider
from .coaninet import CoaninetProvider
from .doramyworld import DoramyWorldProvider
from .eneyida import EneyidaProvider
from .hentaiukr import HentaiUkrProvider
from .kinotron import KinoTronProvider
from .kinovezha import KinoVezhaProvider
from .klontv import KlonTVProvider
from .serialno import SerialnoProvider
from .simpsonsuatv import SimpsonsUATvProvider
from .uaflix import UAFlixProvider
from .uakino import UakinoProvider
from .uaserialspro import UASerialsProProvider
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
    register(CoaninetProvider())
    register(EneyidaProvider())
    register(KlonTVProvider())
    register(SerialnoProvider())
    register(DoramyWorldProvider())
    register(UASerialsProProvider())
    register(AnitubeinuaProvider())
    register(SimpsonsUATvProvider())
    register(AnimeONProvider())


bootstrap()
