"""Upstream drift monitor (spec #285, tickets #286–#289).

A standalone, detached probe: nightly listing probes for all plain-HTTP
providers plus a rotating deep probe (content → stream → HEAD), verdicts
against a self-calibrating baseline, and GitHub-issue filing on two
consecutive failures. Never shares state with the API process.
"""
