"""The native surface (``/api/*``) conversations.

The native contract's route conversations live here as modules with a
``register(app)`` entry point — the same pattern the facade split
proved (``jellyfin/*.py``, PR #411). ``main.py`` keeps only assembly:
app wiring, middleware, health/providers/sections/search/home/poster.

  - ``content`` — the content conversation: browse, the content
    discriminator, and stream resolution (2026-09-08 architecture
    review, candidate 1).
"""
