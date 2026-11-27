r"""Conservative reader for BibLaTeX/Biber-generated .bbl source files.

Converts supported \entry, \field, \name, \list and verbatim field records
into the existing BibEntry contract. No TeX execution or expansion is done.
Malformed/unsupported records generate diagnostics instead of guessed data.
"""
from __future__ import annotations

import re

from .contracts import BibEntry

_MAX_CHARS = 64 * 1024 * 1024
_MAX_ENTRIES = 30000
_MAX_DEPTH = 128
_ENTRY = re.compile(r"\\entry(?![A-Za-z@])")
_END_ENTRY = re.compile(r"\\endentry(?![A-Za-z@])")
_COMMAND = re.compile(r"\\(field|name|list|verb)(?![A-Za-z@])")
_NAME_PART = re.compile(r"(?<![A-Za-z])([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?=\{)")


def _without_comments(source: str) -> str:
    """Drop TeX comments but preserve literal percent in Biber verbatim fields."""
    result = []
    pos = 0
    in_verbatim = False
    while pos < len(source):
        if in_verbatim and source.startswith(r"\endverb", pos):
            in_verbatim = False
        if not in_verbatim and source.startswith(r"\verb", pos):
            following = pos + len(r"\verb")
            if following == len(source) or not source[following].isalpha():
                tail = following
                while tail < len(source) and source[tail].isspace():
                    tail += 1
                if tail < len(source) and source[tail] != "{":
                    in_verbatim = True
        if source[pos] == "%" and not in_verbatim:
            backslashes = 0
            j = pos - 1
            while j >= 0 and source[j] == "\\":
                backslashes += 1
                j -= 1
            if backslashes % 2 == 0:
                # Retain newline for diagnostics, not comments.
                while pos < len(source) and source[pos] != "\n":
                    pos += 1
                continue
        result.append(source[pos])
        pos += 1
    return "".join(result)


def _group(text: str, start: int) -> tuple[str, int]:
    """Read one balanced TeX brace group; preserve nesting and escaped braces."""
    if start >= len(text) or text[start] != "{":
        raise ValueError("expected braced group")
    depth = 1
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
            if depth > _MAX_DEPTH:
                raise ValueError("brace depth limit")
        elif ch == "}":
            depth -= 1
            if not depth:
                return text[start + 1:i], i + 1
        i += 1
    raise ValueError("unclosed brace group")


def _arguments(text: str, pos: int, count: int) -> tuple[list[str], int]:
    args = []
    for _ in range(count):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        value, pos = _group(text, pos)
        args.append(value)
    return args, pos


def _top_groups(text: str) -> list[str]:
    result = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
        elif text[pos] == "{":
            item, pos = _group(text, pos)
            result.append(item)
        else:
            raise ValueError("unexpected tokens in names/list field")
    return result


def _name_parts(text: str) -> dict[str, str]:
    result = {}
    for match in _NAME_PART.finditer(text):
        try:
            value, _ = _group(text, match.end())
        except ValueError:
            continue
        result[match.group(1).lower()] = value
    return result


def _names(payload: str) -> list[str]:
    authors = []
    for name in _top_groups(payload):
        parts = _name_parts(name)
        family = parts.get("family") or parts.get("last")
        given = parts.get("given") or parts.get("first")
        prefix = parts.get("prefix", "")
        suffix = parts.get("suffix", "")
        if family:
            full_family = " ".join(v for v in (prefix, family) if v)
            human = ", ".join(v for v in (full_family, given, suffix) if v)
            human = re.sub(r"\\bibinitperiod\b", ".", human)
            human = re.sub(r"\\bibnamedelim[a-z]*\b", " ", human)
            authors.append(human)
    return authors


def _verb_field(body: str, start: int) -> tuple[str, str, int]:
    r"""Read Biber's \verb{field}\verb raw data\endverb representation."""
    args, pos = _arguments(body, start, 1)
    name = args[0].strip().lower()
    while pos < len(body) and body[pos].isspace():
        pos += 1
    if body.startswith(r"\verb", pos):
        pos += len(r"\verb")
        if pos < len(body) and body[pos].isalpha():
            raise ValueError("malformed verb data")
    elif pos < len(body) and body[pos] == "{":
        value, pos = _group(body, pos)
        return name, value, pos
    else:
        raise ValueError("missing verb payload")
    end = body.find(r"\endverb", pos)
    if end < 0:
        raise ValueError("missing endverb")
    return name, body[pos:end].strip(), end + len(r"\endverb")


def _read_entry(block: str, key: str, etype: str) -> tuple[BibEntry, list[str]]:
    fields: dict[str, object] = {}
    warnings = []
    pos = 0
    while m := _COMMAND.search(block, pos):
        kind = m.group(1)
        try:
            if kind == "field":
                (name, value), pos = _arguments(block, m.end(), 2)
                name = name.strip().lower()
                if name and name not in fields:
                    fields[name] = value
            elif kind == "verb":
                name, value, pos = _verb_field(block, m.end())
                if name and name not in fields:
                    fields[name] = value
            elif kind == "name":
                (name, _count, _opts, payload), pos = _arguments(block, m.end(), 4)
                if name.strip().lower() == "author":
                    people = _names(payload)
                    if people and "author" not in fields:
                        fields["author"] = " and ".join(people)
                    elif not people and payload.strip():
                        warnings.append(f"BBL_NAMES_UNREADABLE key={key}")
            else:  # \list{publisher}{2}{{First}{Second}}
                (name, _count, payload), pos = _arguments(block, m.end(), 3)
                name = name.strip().lower()
                if name and name not in fields:
                    fields[name] = " and ".join(_top_groups(payload))
        except ValueError as exc:
            warnings.append(f"BBL_FIELD_UNREADABLE key={key} command={kind}: {exc}")
            pos = m.end()
    if "url" not in fields and isinstance(fields.get("urlraw"), str):
        fields["url"] = fields["urlraw"]
    return BibEntry(original_key=key, entry_type=etype, fields=fields), warnings


def parse_biblatex_bbl(text: str) -> tuple[list[BibEntry], list[str]]:
    """Parse a supported Biber BBL subset, returning entries and diagnostics.

    This is not full BibLaTeX coverage: malformed records and unsupported
    metadata are reported; other intact records are kept for partial recovery.
    """
    if len(text) > _MAX_CHARS:
        return [], ["BBL_SIZE_LIMIT: input exceeds 64 MiB"]
    clean = _without_comments(text)
    entries = []
    warnings = []
    position = 0
    while match := _ENTRY.search(clean, position):
        if len(entries) >= _MAX_ENTRIES:
            warnings.append("BBL_ENTRY_LIMIT: over 30000 entries")
            break
        try:
            (key, entry_type, _opts), start = _arguments(clean, match.end(), 3)
        except ValueError as exc:
            warnings.append(f"BBL_MALFORMED_ENTRY offset={match.start()}: {exc}")
            position = match.end()
            continue
        key = key.strip()
        entry_type = entry_type.strip().lower()
        end = _END_ENTRY.search(clean, start)
        if end is None:
            warnings.append(f"BBL_TRUNCATED_ENTRY key={key or '?'}")
            break
        position = end.end()
        if not key or not entry_type:
            warnings.append(f"BBL_MALFORMED_ENTRY offset={match.start()}: empty key/type")
            continue
        try:
            entry, issues = _read_entry(clean[start:end.start()], key, entry_type)
            entries.append(entry)
            warnings.extend(issues)
        except ValueError as exc:
            warnings.append(f"BBL_MALFORMED_ENTRY key={key}: {exc}")
    if not entries and not warnings and clean.strip():
        warnings.append("BBL_NO_ENTRIES: no Biber entries found")
    return entries, warnings
