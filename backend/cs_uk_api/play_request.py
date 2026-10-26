"""The play-request seam: one provider attempt, one recording policy.

Every play press reaches this module before it reaches a provider:

  - native ``/api/stream/{id}`` parses its provider-scoped id here;
  - the Jellyfin delivery seam resolves its playable id family to one or
    more members and asks this module per member.

A request is intentionally narrower than a ROUTE: this module owns the
provider call, the episode-translation gate, the two-axis recording
(``health`` for lane faults and ``play_attestation`` for the play outcome)
and the typed served/refused answer. The routes keep their own envelopes
— native 400/404/502, facade 404 — so ADR-0002 is rendered where it
belongs, not re-litigated here.

The refusal-code mapper exists for exactly that reason: a surface passes
the code its client will be handed, so the attestation records the code
the caller actually saw. Jellyfin passes the provider verdict unchanged;
native maps its 502 envelope to ``upstream_unreachable``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from .health import ITEM_VERDICT_CODES, TRACKER, record_verdict
from .models import PlaySurface, StreamResponse
from .play_attestation import PLAY_ATTESTATION
from .providers import PROVIDERS
from .providers.base import BaseProvider, ProviderError
from .torrent_engine import ENGINE_TRACKER_ID
from .wire_identity import split_wire_id

log = logging.getLogger(__name__)

#: Maps a provider verdict to the refusal code the calling surface will
#: hand its client. ``None`` means a codeless crash.
RefusalCode = Callable[[str | None], str | None]


@dataclass(frozen=True)
class PlayAttempt:
    """One provider attempt's typed outcome.

    ``stream is None`` is a refusal; ``code`` is the provider's verdict
    (None for a codeless crash). ``error`` is retained so the surface can
    render its own message without guessing one.
    """

    stream: StreamResponse | None
    provider_id: str | None
    code: str | None
    error: Exception | None = None

    @property
    def served(self) -> bool:
        """True when this attempt produced a playable stream."""
        return self.stream is not None


def _same_code(code: str | None) -> str | None:
    return code


def playable_target(item_id: str) -> tuple[str, str] | None:
    """``(provider_id, external_id)`` for a provider-scoped playable id.

    The native and facade episode paths share this split: the first
    ``:`` separates the provider, the rest is the provider's external id
    (which may itself contain colons). An unknown provider or an empty
    external id is not a playable target.
    """
    provider_id, external_id = split_wire_id(item_id)
    if provider_id in PROVIDERS and external_id:
        return provider_id, external_id
    return None


def attest_refusal(
    provider_id: str | None,
    item_id: str,
    code: str | None,
    surface: PlaySurface | None,
) -> None:
    """Record a refusal when the caller is a playback surface.

    ``surface is None`` is the subtitle route's mode: it crosses the seam
    to find a track, so its outcome is not a play request.
    """
    if surface is not None:
        PLAY_ATTESTATION.refused(provider_id, item_id, code, surface)


async def _translation_error(
    provider: BaseProvider,
    external_id: str,
    translation: str | None,
    http: httpx.AsyncClient,
) -> ProviderError | None:
    """Validate an episode translation before spending a stream call.

    Providers that do not implement per-episode translations return None
    from the hook, which means "not applicable". A hook that raises is a
    diagnostic failure, not a client verdict: log it and accept the
    translation, the same fail-open posture the native route always had.
    """
    if translation is None:
        return None
    try:
        allowed = await provider.episode_translations(external_id, http)
    except Exception:
        log.warning(
            "episode_translations(%s) failed; accepting any translation",
            provider.id,
            exc_info=True,
        )
        return None
    if allowed is not None and translation not in allowed:
        return ProviderError(
            "invalid_translation", f"{translation} not in {allowed}"
        )
    return None


async def attempt_play(
    provider_id: str,
    external_id: str,
    translation: str | None,
    http: httpx.AsyncClient,
    *,
    item_id: str,
    surface: PlaySurface | None,
    refusal_code: RefusalCode = _same_code,
) -> PlayAttempt:
    """Run one provider attempt and record both outcome axes.

    This is the single call site for ``provider.stream()`` in playback:
    the translation gate, lane-health classification, engine-path
    attribution and play attestation live here, not in either surface.

    ``item_id`` is the wire id the client asked for (the attestation's
    identity); ``refusal_code`` is the code that surface will render.
    """
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        # Defensive: callers resolve registered providers first.
        attest_refusal(None, item_id, refusal_code("not_found"), surface)
        return PlayAttempt(None, None, "not_found")

    gate_error = await _translation_error(provider, external_id, translation, http)
    if gate_error is not None:
        attest_refusal(provider_id, item_id, refusal_code(gate_error.code), surface)
        return PlayAttempt(None, provider_id, gate_error.code, gate_error)

    try:
        stream = await provider.stream(external_id, translation, http)
    except ProviderError as error:
        attributed = ENGINE_TRACKER_ID if error.engine_path else provider_id
        if error.code in ITEM_VERDICT_CODES:
            # Deterministic item/client-level verdicts never flip the
            # tracker (ADR-0002, #373): a dead title is not a dead lane.
            log.info(
                "playback item verdict=%s provider=%s id=%s",
                error.code,
                provider_id,
                item_id,
            )
        else:
            log.warning(
                "playback stream failed provider=%s id=%s err=%s",
                provider_id,
                item_id,
                error,
            )
            record_verdict(attributed, error.code)
        attest_refusal(attributed, item_id, refusal_code(error.code), surface)
        return PlayAttempt(None, attributed, error.code, error)
    except Exception as error:  # noqa: BLE001 — never surface a 5xx on a play
        log.warning(
            "playback stream failed provider=%s id=%s err=%s",
            provider_id,
            item_id,
            error,
        )
        record_verdict(provider_id, None)
        attest_refusal(provider_id, item_id, refusal_code(None), surface)
        return PlayAttempt(None, provider_id, None, error)

    TRACKER.record(provider_id, ok=True)
    if surface is not None:
        PLAY_ATTESTATION.served(provider_id)
    return PlayAttempt(stream, provider_id, None)


__all__ = [
    "PlayAttempt",
    "RefusalCode",
    "attempt_play",
    "attest_refusal",
    "playable_target",
]
