"""Backend provider registry (ADR-0004, issue #85).

The registry is the authoritative active-provider list. Lifecycle rules:

  - Registration is via hardcoded ``register(...)`` calls in
    ``bootstrap()`` below. There is no hot-reload, no config-file-driven
    registration, no runtime plugin discovery — providers are deployed
    code.
  - The order of ``register(...)`` calls is meaningful: ``/api/search``
    returns the flattened results in ``PROVIDERS.values()`` order. No
    priority field, no secondary sort.
  - Retirement is by COMMENTING OUT the relevant ``register(...)`` call.
    The adapter source remains in the tree for historical context or
    possible reactivation. Once commented out, the provider does not
    appear in ``/api/providers``, ``/api/sections``, ``/api/search``,
    or ``/api/browse`` (any of which would return 400 ``unknown_provider``).
    This is the convention already used for Banderakino.
  - Health tracking is owned by issue #53 (sliding window + startup
    marker), unchanged by ADR-0004.

To add a provider, follow the eight-step checklist in ``docs/status.md``:
  1. Create the adapter under ``cs_uk_api/providers/`` inheriting from
     ``BaseProvider``.
  2. Add a live-captured fixture under ``cs_uk_api/tests/fixtures/``.
  3. Implement ``search()``, ``content()``, ``stream()`` (and optionally
     ``browse()`` if the provider supports section browsing).
  4. Add a provider-specific test file under ``cs_uk_api/tests/``.
  5. Wire each tag's tests to exercise the live behavior.
  6. Run the live gate (search + content + stream).
  7. Add the ``import`` and ``register(...)`` call below.
  8. Update provider triage in ``docs/status.md``.
"""
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
    # Register order is meaningful: /api/search returns the flattened
    # provider results in PROVIDERS.values() order. To retire a provider,
    # comment the call out (do NOT delete the adapter source).
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
