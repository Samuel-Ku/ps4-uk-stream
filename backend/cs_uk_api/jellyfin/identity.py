"""The facade's stable-identity conversation (D4/D8 support).

ONE owner for what the server tells the client it is and how it names
itself on the wire: the genuine-Jellyfin product/version pair the
official apps validate on connect, the stateless "signed in as" label
(D8), and the stable per-process ``ServerId`` clients pin in their
local database. Every handshake, session and dashboard route borrows
these — carving them out keeps ``router.py`` about ROUTES, not identity
math (locality: one change to identity lands here).

Import direction: this module imports config only — never the router.
"""

from __future__ import annotations

import hashlib

from ..config import SETTINGS

#: What the server tells the client it is. The official Jellyfin apps
#: validate the server's product/version on connect and refuse anything
#: that doesn't look like a real Jellyfin ("unsupported version or
#: product"). Surface a genuine Jellyfin identity so any client accepts
#: the handshake; the facade itself is version-agnostic.
_PRODUCT = "Jellyfin Server"
_VERSION = "10.11.11"


def _user_name_for(user_id: str) -> str:
    """The display name backed by a remembered ``/Users/{id}`` check.

    The facade is stateless and cannot recall what the user typed at
    login; a stable label ("User") keeps every client's "signed in as"
    UI consistent without persisting anything (D8).
    """
    return "User"


def _server_id() -> str:
    """Stable per-process identity: deterministic hash of host:port.

    A restart keeps the same ServerId (clients pin it in their local
    database), while two different deployments differ.
    """
    return hashlib.sha256(f"{SETTINGS.host}:{SETTINGS.port}".encode()).hexdigest()[:16]


__all__ = ["_PRODUCT", "_VERSION", "_server_id", "_user_name_for"]
