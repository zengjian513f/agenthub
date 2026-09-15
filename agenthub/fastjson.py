"""`loads` that uses orjson when it is installed and the standard library otherwise.

Session files are parsed one JSON line at a time; on the real 52 MB Claude and
228 MB Codex files orjson.loads is 2.6–2.8× faster than json.loads with identical
results. orjson is an optional dependency (prebuilt wheels for Linux, macOS and
Windows): a node without it runs the same code on the standard library.

orjson rejects a few inputs the standard library accepts (NaN/Infinity literals,
some trailing garbage cases); those fall back to json.loads so the value — or the
exception — is always the standard library's. One documented difference stays:
orjson returns integers beyond 64 bits as floats (the standard library keeps
them exact); guarding against that with a digit scan cost more than orjson
saved, and no CLI writes such numbers into a session file.
"""
from __future__ import annotations

import json

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - depends on the environment
    _orjson = None

AVAILABLE = _orjson is not None


def loads(text, **kwargs):
    """Parse JSON text or bytes; same contract as json.loads."""
    if _orjson is None or kwargs:
        return json.loads(text, **kwargs)
    try:
        return _orjson.loads(text)
    except _orjson.JSONDecodeError:
        return json.loads(text)
