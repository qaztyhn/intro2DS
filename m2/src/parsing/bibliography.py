"""Bibliography extraction (TV3).

Stage 2: raw .bib parsing. Field normalization, \\bibitem parsing and
cross-version unification are added in later stages.
"""
from __future__ import annotations

import logging
import re

from .contracts import BibEntry, BibliographyResult, GatheredDocument  # noqa: F401

log = logging.getLogger(__name__)

_ENTRY_START = re.compile(r"@\s*([A-Za-z]+)\s*([{(])")
_KEY = re.compile(r"[^\s,{}]+")
_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_\-:.]*")
_TOKEN = re.compile(r"[^\s,{}#\"=()]+")
_SKIP_TYPES = {"comment", "preamble"}
_MONTHS = {
    "jan": "January", "feb": "February", "mar": "March", "apr": "April",
    "may": "May", "jun": "June", "jul": "July", "aug": "August",
    "sep": "September", "oct": "October", "nov": "November", "dec": "December",
}


def _skip_ws(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    return i


def _read_braced(s: str, i: int) -> tuple[str, int]:
    """s[i] == '{'. Return (inner content, index after the closing brace)."""
    depth, start, n = 0, i + 1, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
        i += 1
    raise ValueError("unbalanced braces")


def _read_quoted(s: str, i: int) -> tuple[str, int]:
    """s[i] == '"'. Quotes inside braces do not terminate the value."""
    depth, start, n = 0, i + 1, len(s)
    i += 1
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == '"' and depth == 0:
            return s[start:i], i + 1
        i += 1
    raise ValueError("unterminated quoted value")


def _read_value(s: str, i: int, strings: dict[str, str]) -> tuple[str, int]:
    parts: list[str] = []
    while True:
        i = _skip_ws(s, i)
        if i >= len(s):
            raise ValueError("unexpected end of input in value")
        c = s[i]
        if c == "{":
            text, i = _read_braced(s, i)
        elif c == '"':
            text, i = _read_quoted(s, i)
        else:
            m = _TOKEN.match(s, i)
            if not m:
                raise ValueError(f"bad value at offset {i}")
            tok, i = m.group(0), m.end()
            text = tok if tok.isdigit() else strings.get(tok.lower(), tok)
        parts.append(text)
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == "#":
            i += 1
            continue
        return "".join(parts), i


def _read_fields(
    s: str, i: int, close_ch: str, strings: dict[str, str],
    warnings: list[str], key: str,
) -> tuple[dict[str, str], int]:
    fields: dict[str, str] = {}
    n = len(s)
    while True:
        i = _skip_ws(s, i)
        while i < n and s[i] == ",":
            i = _skip_ws(s, i + 1)
        if i >= n:
            raise ValueError("unterminated entry")
        if s[i] == close_ch:
            return fields, i + 1
        m = _FIELD_NAME.match(s, i)
        if not m:
            raise ValueError(f"bad field name at offset {i}")
        name = m.group(0).lower()
        i = _skip_ws(s, m.end())
        if i >= n or s[i] != "=":
            raise ValueError(f"missing '=' after field '{name}'")
        value, i = _read_value(s, i + 1, strings)
        if name in fields:
            warnings.append(f"{key}: duplicate field '{name}' ignored (first kept)")
        else:
            fields[name] = value.strip()


def parse_bib(text: str) -> tuple[list[BibEntry], list[str]]:
    """Parse BibTeX text into raw entries. Returns (entries, warnings)."""
    entries: list[BibEntry] = []
    warnings: list[str] = []
    strings = dict(_MONTHS)
    pos = 0
    while True:
        m = _ENTRY_START.search(text, pos)
        if not m:
            break
        kind = m.group(1).lower()
        open_ch = m.group(2)
        close_ch = "}" if open_ch == "{" else ")"
        i = m.end()
        pos = i  # on error, resume scanning right after this '@type{'
        key = ""
        try:
            if kind in _SKIP_TYPES:
                if open_ch == "{":
                    _, pos = _read_braced(text, i - 1)
                continue
            if kind == "string":
                fields, pos = _read_fields(text, i, close_ch, strings, warnings, "@string")
                for name, value in fields.items():
                    strings[name.lower()] = value
                continue
            i = _skip_ws(text, i)
            km = _KEY.match(text, i)
            key = km.group(0) if km else ""
            if km:
                i = km.end()
            fields, pos = _read_fields(text, i, close_ch, strings, warnings, key)
            entries.append(BibEntry(original_key=key, entry_type=kind, fields=fields))
        except ValueError as exc:
            warnings.append(f"malformed @{kind} entry '{key}': {exc}")
    return entries, warnings


def extract_bibliography(document: GatheredDocument) -> list[BibEntry]:
    """Extract raw bibliography entries of one version (.bib files only for now)."""
    entries: list[BibEntry] = []
    for path in sorted(document.bib_files):
        parsed, warns = parse_bib(document.bib_files[path])
        for w in warns:
            log.warning("[%s v%s] stage=bib_parse file=%s: %s",
                        document.arxiv_id, document.version, path, w)
        entries.extend(parsed)
    return entries