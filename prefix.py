"""Printed cron prefix for the active Hermes profile."""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import re

_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_NAME: str | None = None


def remember_profile_name(reported: object) -> None:
    """Store ctx.profile_name. An unusable value is forgotten."""
    global _NAME
    if isinstance(reported, str) and _PROFILE.fullmatch(reported.strip()):
        _NAME = reported.strip()
        return
    _NAME = None


def cron_prefix() -> str:
    name = _NAME or "default"
    if name == "default":
        return "hermes"
    return f"hermes -p {name}"


def cron_lines(job_id: str | None = None) -> list[str]:
    """Only the prefixed form. A non-default profile never also prints a bare line."""
    prefix = cron_prefix()
    lines = [f"{prefix} cron list", f"{prefix} cron status"]
    if job_id:
        lines.append(f"{prefix} cron remove {job_id}")
    return lines
