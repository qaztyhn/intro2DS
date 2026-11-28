r"""Bounded reader for simple AMSRefs \bib{key}{type}{fields} records.

Repeated author/editor fields become ordered BibTeX name lists. Nested AMSRefs
metadata (for example book={...}) is deliberately not flattened or guessed.
"""
from __future__ import annotations

import re

from .biblatex_bbl import (_arguments, _group, _without_comments,
                           _MAX_CHARS, _MAX_ENTRIES, entries_to_bibtex)
from .contracts import BibEntry

_BIB = re.compile(r"\\bib(?![A-Za-z@])")
_FIELD = re.compile(r"([A-Za-z][A-Za-z0-9_-]*)\s*=\s*")
_NESTED = re.compile(r"(?:^|[,;\s])[A-Za-z][A-Za-z0-9_-]*\s*=\s*\{")
_SCALARS = {'title', 'subtitle', 'date', 'year', 'journal', 'publisher',
            'volume', 'number', 'pages', 'series', 'isbn', 'issn', 'doi', 'url',
            'booktitle', 'edition', 'note', 'eprint', 'language', 'address', 'type', 'review'}


def is_amsrefs_bbl(text: str) -> bool:
    """Recognize active AMSRefs record commands, excluding comments."""
    return bool(_BIB.search(_without_comments(text)))


def _fields(payload: str, key: str) -> tuple[dict[str, str], list[str]]:
    result = {}
    people: dict[str, list[str]] = {}
    warnings = []
    pos = 0
    while pos < len(payload):
        while pos < len(payload) and (payload[pos].isspace() or payload[pos] == ','):
            pos += 1
        if pos == len(payload):
            break
        match = _FIELD.match(payload, pos)
        if match is None:
            warnings.append(f'AMSREFS_FIELD_UNREADABLE key={key} offset={pos}')
            break
        name = match[1].lower()
        try:
            value, pos = _group(payload, match.end())
        except ValueError as exc:
            warnings.append(f'AMSREFS_FIELD_UNREADABLE key={key} field={name}: {exc}')
            break
        if name not in _SCALARS and name not in ('author', 'editor'):
            warnings.append(f'AMSREFS_UNSUPPORTED_FIELD key={key} field={name}')
            continue
        if _NESTED.search(value):
            warnings.append(f'AMSREFS_NESTED_FIELD key={key} field={name}')
            continue
        if name in ('author', 'editor'):
            people.setdefault(name, []).append(value.strip())
        elif name in result and result[name] != value:
            warnings.append(f'AMSREFS_DUPLICATE_FIELD key={key} field={name}: first kept')
        else:
            result[name] = value
    for name, names in people.items():
        result[name] = ' and '.join(names)
    return result, warnings


def parse_amsrefs_bbl(text: str) -> tuple[list[BibEntry], list[str]]:
    """Read the supported AMSRefs subset, with diagnostics for partial records."""
    if len(text) > _MAX_CHARS:
        return [], ['AMSREFS_SIZE_LIMIT: input exceeds 64 MiB']
    clean = _without_comments(text)
    entries, warnings = [], []
    pos = 0
    attempts = 0
    while match := _BIB.search(clean, pos):
        attempts += 1
        if attempts > _MAX_ENTRIES:
            warnings.append('AMSREFS_ENTRY_LIMIT: over 30000 records')
            break
        try:
            (key, kind, payload), end = _arguments(clean, match.end(), 3)
        except ValueError as exc:
            warnings.append(f'AMSREFS_MALFORMED_ENTRY offset={match.start()}: {exc}')
            pos = match.end()
            continue
        following = _BIB.search(clean, match.end())
        if following and following.start() < end:
            warnings.append(f'AMSREFS_MALFORMED_ENTRY key={key}: nested record command')
            pos = following.start()
            continue
        pos = end
        key, kind = key.strip(), kind.strip().lower()
        if not key or not kind:
            warnings.append(f'AMSREFS_MALFORMED_ENTRY offset={match.start()}: empty key/type')
            continue
        fields, issues = _fields(payload, key)
        warnings.extend(issues)
        if fields:
            entries.append(BibEntry(key, kind, fields))
        else:
            warnings.append(f'AMSREFS_EMPTY_ENTRY key={key}')
    if not entries and not warnings and clean.strip():
        warnings.append('AMSREFS_NO_ENTRIES: no AMSRefs records found')
    return entries, warnings


def amsrefs_bbl_to_bibtex(text: str) -> tuple[str, list[str]]:
    """Return derived BibTeX and retained parse/serialization diagnostics."""
    entries, warnings = parse_amsrefs_bbl(text)
    rendered, issues = entries_to_bibtex(entries)
    return rendered, warnings + issues
