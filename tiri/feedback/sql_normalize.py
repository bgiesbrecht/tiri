"""SQL normalization for benchmark comparison.

Rules per docs/feedback.md:
- Lowercase keywords (and identifiers — anything outside string/identifier quotes)
- Collapse whitespace (spaces / tabs / newlines) to single spaces
- Strip leading/trailing whitespace
- Remove trailing semicolons
- Do NOT normalize string literals or quoted identifiers (single quotes or
  double quotes)

This is a pragmatic implementation, not a full SQL parser. It treats `'…'`
and `"…"` as opaque (preserves casing inside) and lowercases everything else.
"""

from __future__ import annotations

import re


_WHITESPACE = re.compile(r"\s+")


def normalize_sql(sql: str) -> str:
    """Apply the doc's normalization rules; return the canonical form."""
    # Pass 1: walk the string and lowercase everything outside quoted spans.
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            # Find the matching close, handling doubled quotes as escapes.
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = sql[i]
                out.append(c)
                i += 1
                if c == quote:
                    if i < n and sql[i] == quote:
                        # Doubled quote = escaped literal, keep both.
                        out.append(quote)
                        i += 1
                        continue
                    break
            continue
        out.append(ch.lower())
        i += 1
    lowered = "".join(out)

    # Pass 2: collapse whitespace, strip, drop trailing semicolons.
    collapsed = _WHITESPACE.sub(" ", lowered).strip()
    while collapsed.endswith(";"):
        collapsed = collapsed[:-1].rstrip()
    return collapsed
