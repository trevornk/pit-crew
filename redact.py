"""Privacy helpers: VIN / token / coordinate scrubbing and the patterns registered with Hermes.

``scrub`` is applied to anything the plugin logs or returns that could have come from a remote
service (error messages, adapter notes). Hermes' own redactor is extended with
:data:`HERMES_PATTERNS` through ``ctx.register_redaction_patterns`` so VINs and tokens are masked
in transcripts and logs too.
"""

from __future__ import annotations

import re
from typing import List

# Hermes requires each registered pattern to start with >= 2 literal characters, so VIN coverage
# there is by well-known EV maker prefixes; ``scrub`` below catches any 17-char VIN shape.
HERMES_PATTERNS: List[str] = [
    r"5YJ[A-HJ-NPR-Z0-9]{14}",      # Tesla (Fremont)
    r"7SA[A-HJ-NPR-Z0-9]{14}",      # Tesla (Fremont, Model Y)
    r"7G2[A-HJ-NPR-Z0-9]{14}",      # Tesla (Cybertruck/Semi)
    r"LRW[A-HJ-NPR-Z0-9]{14}",      # Tesla (Shanghai)
    r"XP7[A-HJ-NPR-Z0-9]{14}",      # Tesla (Berlin)
    r"SFZ[A-HJ-NPR-Z0-9]{14}",      # Tesla (Roadster)
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",  # JWT / HA long-lived token
]

_VIN = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}\b")
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_BEARER = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{12,}")
_COORD_KV = re.compile(r"(?i)([\"']?(?:lat|latitude|lon|lng|longitude)[\"']?\s*[:=]\s*)-?\d{1,3}\.\d{2,}")
_COORD_PAIR = re.compile(r"(?<!\d)-?\d{1,2}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}(?!\d)")


def scrub(text: object) -> str:
    """Mask VINs, tokens and coordinates in free text."""
    s = str(text)
    s = _JWT.sub("[token]", s)
    s = _BEARER.sub(lambda m: f"{m.group(1)} [token]", s)
    s = _COORD_KV.sub(lambda m: f"{m.group(1)}[redacted]", s)
    s = _COORD_PAIR.sub("[location redacted]", s)
    s = _VIN.sub("[VIN]", s)
    return s


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def plain(text: object, limit: int = 60) -> str:
    """Untrusted text (a Home Assistant zone or entity name, a car's name) made safe to put in a prompt or an alert:
    no control characters or line breaks (so it cannot fake extra lines such as "Approve only if..."), collapsed
    spaces, a length limit, and the usual secret scrubbing."""
    s = _CONTROL.sub(" ", str(text))
    s = " ".join(s.split())
    s = scrub(s)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"
