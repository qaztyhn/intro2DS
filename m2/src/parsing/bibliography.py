"""Bibliography extraction (TV3).

Stage 2: raw .bib parsing. Field normalization, \\bibitem parsing and
cross-version unification are added in later stages.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date as calendar_date
from urllib.parse import urlsplit, unquote

from typing import NamedTuple
from collections.abc import Callable
from .contracts import BibEntry, BibliographyResult, GatheredDocument  # noqa: F401
from .biblatex_bbl import is_biblatex_bbl, parse_biblatex_bbl, is_empty_bibliography
from .amsrefs_bbl import is_amsrefs_bbl, parse_amsrefs_bbl

log = logging.getLogger(__name__)

_ENTRY_START = re.compile(r"@\s*([A-Za-z]+)\s*([{(])")
_KEY = re.compile(r"[^\s,{}]+")
_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_\-:.]*")
_TOKEN = re.compile(r"[^\s,{}#\"=()]+")
_RECOVERY_FIELD = re.compile(r'(?m)^[ \t]*([A-Za-z][A-Za-z0-9_\-:.]*)\s*=')
_RECOVERY_ENTRY = re.compile(r'(?m)^[ \t]*@\s*[A-Za-z]+\s*[{(]')
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


def _recover_fields(text, start, strings, key, close_ch):
    """Quarantine malformed fields; keep only independently bounded valid values.

    Recovery requires a title and an author/year/DOI. It never fixes braces or
    invents field values. Line-anchored assignments are resynchronization points;
    A recovered value may not consume a later line's field assignment. Ambiguous
    multi-line values are quarantined rather than swallowing the rest of an entry.
    """
    boundary = _RECOVERY_ENTRY.search(text, start)
    end = boundary.start() if boundary else len(text)
    fragment = text[start:end]
    matches = list(_RECOVERY_FIELD.finditer(fragment))
    fields, warnings = {}, []
    for i, match in enumerate(matches):
        name = match[1].lower()
        next_field = matches[i + 1].start() if i + 1 < len(matches) else len(fragment)
        field_text = fragment[match.end():next_field]
        try:
            value, after = _read_value(field_text, 0, strings)
            tail = ''.join(field_text[after:].split())
            allowed = {'' , ','} if next_field < len(fragment) else {'', ',', close_ch, ',' + close_ch}
            if tail not in allowed:
                raise ValueError('unparsed tokens after value')
            if name not in fields:
                fields[name] = value.strip()
            else:
                warnings.append(f'{key}: duplicate field {name!r} ignored during recovery')
        except ValueError:
            warnings.append(f'{key}: quarantined malformed field {name!r}')
    if not fields.get('title') or not any(fields.get(n) for n in ('author', 'year', 'doi')):
        return None, [], end
    warnings.append(f'{key}: recovered independently valid fields; entry remains partial')
    return fields, warnings, end


def _scan(text: str, strings: dict[str, str], warnings: list[str]) -> list[BibEntry]:
    """Scan entries while collecting string definitions and recovery diagnostics."""
    entries: list[BibEntry] = []
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
        line_start = text.rfind("\n", 0, m.start()) + 1
        if "%" in text[line_start:m.start()]:
            continue  # '@' on a %-commented line: ignored (biber semantics)
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
            if key and kind not in _SKIP_TYPES and kind != 'string':
                recovered, details, resume = _recover_fields(text, km.end(), strings, key, close_ch)
                if recovered is not None:
                    entries.append(BibEntry(key, kind, recovered))
                    warnings.extend(details)
                    pos = resume
    return entries


def parse_bib(text: str) -> tuple[list[BibEntry], list[str]]:
    """Collect string definitions before parsing entries, including forward uses."""
    strings = dict(_MONTHS)
    _scan(text, strings, [])
    warnings: list[str] = []
    return _scan(text, dict(strings), warnings), warnings


_BOOKTITLE_TYPES = {"inproceedings", "incollection", "inbook", "conference"}


def resolve_crossrefs(entries: list[BibEntry]) -> tuple[list[BibEntry], list[str]]:
    """Fill missing fields from the entry named by `crossref` (case-insensitive).

    The parent's `title` is never copied into the child's `title`; for
    proceedings-like children it becomes `booktitle` when that is missing.
    """
    warnings: list[str] = []
    by_key: dict[str, BibEntry] = {}
    for e in entries:
        if e.original_key:
            by_key.setdefault(e.original_key.casefold(), e)

    resolved: list[BibEntry] = []
    for e in entries:
        fields = dict(e.fields)
        seen = {e.original_key.casefold()}
        cur = e
        for level in range(3):
            ref = str(cur.fields.get("crossref", "")).strip().casefold()
            if not ref:
                break
            parent = by_key.get(ref)
            if parent is None:
                warnings.append(f"{e.original_key}: crossref '{ref}' not found")
                break
            if ref in seen:
                warnings.append(f"{e.original_key}: crossref cycle at '{ref}'")
                break
            seen.add(ref)
            for name, value in parent.fields.items():
                if name == "crossref":
                    continue
                if name == "title":
                    if (level == 0 and e.entry_type in _BOOKTITLE_TYPES
                            and not fields.get("booktitle")):
                        fields["booktitle"] = value
                    continue
                if not fields.get(name):
                    fields[name] = value
            cur = parent
        resolved.append(BibEntry(e.original_key, e.entry_type, fields))
    return resolved, warnings

# --------------------------------------------------------------------------
# \bibitem / thebibliography / .bbl
# --------------------------------------------------------------------------
_COMMENT = re.compile(r"(?<!\\)%[^\n]*")
_URL_ARG = re.compile(r"\\(?:url|href)\s*\{[^{}]*\}")
_BIBITEM_CMD = re.compile(r"\\bibitem(?![A-Za-z])")
_END_BIB = re.compile(r"\\end\s*\{thebibliography\}")
_BIB_CONTEXT = re.compile(r"\\(?:begin|end)\s*\{thebibliography\}")
_BIB_SPACING = re.compile(r"\\(?:BIBentryALTinterwordspacing|BIBentrySTDinterwordspacing|BIBdecl)(?![A-Za-z@])")
_AUTHOR_WRAPPER = re.compile(r"\\(?:bibfnamefont|bibnamefont|citenamefont)(?![A-Za-z@])")
_REPEATED_AUTHOR = re.compile(r"^\s*(?:-{3,}|[\u2013\u2014]{1,3}|\\(?:bysame|BIBdash)(?![A-Za-z@])(?:\s*\{\s*\})?)\s*[.,;:]*\s*$")
_REPEATED_PREFIX = re.compile(r"^\s*(?:-{3,}|[\u2013\u2014]{1,3}|\\(?:bysame|BIBdash)(?![A-Za-z@])(?:\s*\{\s*\})?)")
_NEWBLOCK = re.compile(r"\\newblock\b")
_DOI = re.compile(r"10\.\d{4,9}/[^\s,;{}\]\"<>]+")
_URL = re.compile(r"\\(?:url|href)\s*\{([^{}]*)\}|(https?://[^\s{}\\]+)")
_ARXIV = re.compile(
    r"arXiv\s*:\s*(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})", re.I
)
_YEAR = re.compile(r"(?<![\d.])(?:19|20)\d{2}(?!\d|\.\d)")
_PAGES = re.compile(
    r"\b(?:pp?\.\s*|pp?\s+|pages?\s+)~?\s*\d+(?:\s*(?:-{1,3}|\u2013|\u2014)\s*\d+)?"
    r"|(?<![\d.])\d{1,6}\s*(?:-{1,3}|\u2013|\u2014)\s*\d{1,6}"
    r"(?!\d|\s*[-\u2013\u2014]\s*\d)",
    re.I,
)
_VENUE_HINT = re.compile(
    r"^\s*in\b|\b(?:conference|workshop|symposium|congress|meeting|colloquium)\b",
    re.I,
)


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _strip_comments(text: str) -> str:
    """Remove LaTeX comments but keep '%' inside \\url{...} / \\href{...}."""
    saved: list[str] = []

    def protect(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"

    text = _COMMENT.sub("", _URL_ARG.sub(protect, text))
    return re.sub(r"\x00(\d+)\x00", lambda m: saved[int(m.group(1))], text)


def _venue_text(block: str) -> str:
    m = _EMPH.search(block)
    text = m.group(1) if m else block
    return re.sub(r"^\s*in\b\s*:?\s*", "", text, flags=re.I).strip()


def _bibitem_venue(block: str) -> str:
    """Keep a venue name, excluding recognizable volume/pages/year suffixes."""
    grouped = re.match(r'\s*(?:in\s+)?\{\s*\\em\s+', block, re.I)
    if grouped:
        try:
            inner, end = _read_braced(block, block.index('{'))
            tail = block[end:]
            if re.match(r'\s*(?:,\s*\d+\s*)?\((?:19|20)\d{2}\)', tail):
                name = clean_text(re.sub(r'^\s*\\em\s+', '', inner)).replace(r'\/', '').strip()
                name = re.sub(r'\s+abs/\d{4}\.\d{4,5}(?:v\d+)?$', '', name)
                if not _VENUE_HINT.search(name):
                    name = re.sub(r'\s+\d+$', '', name)
                return name.rstrip(' ,;')
        except ValueError:
            pass
    text = clean_text(_venue_text(block))
    if re.fullmatch(r'https?://\S+\.?', text, flags=re.I):
        return ''  # A repository link is not a journal name.
    suffix = re.search(r',\s*(?:(?:vol(?:ume)?|no|pp?|pages?)\.?\s|'
                       r'\d+\s*(?:\([^)]{0,40}\))?\s*[:;,]|(?:19|20)\d{2}\b)', text, re.I)
    if suffix:
        text = text[:suffix.start()]
    if (re.match(r'\s*\{\\em\s', block)
            and re.search(r'\}\s*,\s*\d+\s*\(((?:19|20)\d{2})\)', block)):
        # The explicit outer issue/date identifies the trailing number inside
        # this old-style journal group as its volume, not part of the name.
        text = re.sub(r'\s+\d+$', '', text)
    return text.rstrip(' ,;')


def _unwrap_links(text: str) -> str:
    """Render visible href/hyperlink labels without interpreting arbitrary TeX."""
    if '\\href' not in text and '\\hyperlink' not in text:
        return text
    output, pos = [], 0
    for match in re.finditer(r'\\(?:href|hyperlink)(?![A-Za-z@])', text):
        if match.start() < pos:
            continue
        start = _skip_ws(text, match.end())
        if start >= len(text) or text[start] != '{':
            continue
        try:
            _, end = _read_braced(text, start)
            label_start = _skip_ws(text, end)
            if label_start >= len(text) or text[label_start] != '{':
                continue
            label, end = _read_braced(text, label_start)
        except ValueError:
            continue
        output.extend((text[pos:match.start()], label))
        pos = end
    output.append(text[pos:])
    return ''.join(output)


_QUOTED_TITLE = re.compile(
    r"(?:``|\u201c|(?<!\\)\")(.+?)(?:''|\u201d|(?<!\\)\")", re.S)


def _quoted_title_fields(body: str) -> dict[str, str] | None:
    """High-precision split for 'Authors, ``Title,'' \\emph{Venue}, year' styles."""
    m = _QUOTED_TITLE.search(body)
    if not m:
        return None
    title = _collapse(m.group(1)).strip(" ,.;")
    author = _collapse(body[:m.start()]).strip(" ,;:")
    if len(title) < 5 or len(title) > 300 or len(author) < 2:
        return None
    out = {"title": title, "author": author}
    after = body[m.end():]
    em = _EMPH.search(after)
    if em:
        venue = _collapse(em.group(1))
        if re.search(r"\bin\s*:?\s*$", after[:em.start()], re.I) or _VENUE_HINT.search(venue):
            out["booktitle"] = _bibitem_venue(venue)
        else:
            out["journal"] = _bibitem_venue(venue)
    return out


def _emphasized_title_fields(body: str) -> dict[str, str] | None:
    """Read the narrow 'initialled authors, emph{{title}}, emph{venue}' style.

    Double braces, comma separation and recognizable personal-name initials are
    required so an emphasized journal after prose is not reclassified as title.
    """
    match = re.search(r'\\emph\s*(?=\{)', body)
    if match is None:
        return None
    prefix = body[:match.start()].strip()
    if not prefix.endswith(','):
        return None
    author = prefix[:-1].strip()
    if not _usable_explicit_bibitem_author(author):
        return None
    names = _authors_from_bibitem(author)
    if not all(re.search(r'(?<![A-Za-z])[A-Z]\.', name) and len(name.split()) >= 2 for name in names):
        return None
    try:
        inner, end = _read_braced(body, match.end())
        inner = inner.strip()
        if not inner.startswith('{'):
            return None
        title, title_end = _read_braced(inner, 0)
        if inner[title_end:].strip() or not 5 <= len(title) <= 1000:
            return None
    except ValueError:
        return None
    after = _unwrap_links(body[end:])
    if not after.lstrip().startswith(','):
        return None
    venue = _EMPH.search(after)
    if venue is None:
        return None
    out = {'author': author, 'title': _collapse(title)}
    venue_name = _bibitem_venue(venue.group())
    if not venue_name:
        return None
    if re.search(r'\bin\s*:?\s*$', after[:venue.start()], re.I) or _VENUE_HINT.search(venue_name):
        out['booktitle'] = venue_name
    else:
        out['journal'] = venue_name
    return out


def _emphasized_venue_year(body: str) -> str | None:
    """Read the explicit venue date even when the author is a collaboration.

    The format must contain a balanced double-braced title, a separate
    emphasized venue, then optional bold volume followed by (publication year).
    No person/corporation name is inferred from the prefix.
    """
    first = re.search(r'\\emph\s*(?=\{)', body)
    if first is None:
        return None
    try:
        title, end = _read_braced(body, first.end())
        title = title.strip()
        if not title.startswith('{'):
            return None
        _, title_end = _read_braced(title, 0)
        if title[title_end:].strip():
            return None
    except ValueError:
        return None
    after = _unwrap_links(body[end:])
    venue = _EMPH.search(after)
    if venue is None:
        return None
    date = re.match(r'\s*(?:\{\\bfseries\s+\d+\}\s*)?\(((?:19|20)\d{2})\)', after[venue.end():])
    return date[1] if date else None


def _emphasized_first_block_fields(block: str) -> dict[str, str] | None:
    """Read initialled authors plus an emphasized title before continuation.

    The suffix must contain only punctuation/numbers (book publication details),
    so a journal embedded after unknown prose is not mistaken for a title.
    """
    match = re.search(r'\\emph\s*(?=\{)', block)
    if match is None:
        return None
    prefix = block[:match.start()].strip()
    if not prefix.endswith(','):
        return None
    author = prefix[:-1].strip()
    if not _usable_explicit_bibitem_author(author):
        return None
    names = _authors_from_bibitem(author)
    if not all(re.search(r'(?<![A-Za-z])[A-Z]\.', name) and len(name.split()) >= 2 for name in names):
        return None
    try:
        title, end = _read_braced(block, match.end())
    except ValueError:
        return None
    if not title.strip() or len(title) > 1000 or not re.fullmatch(r'[\s\d,.;]*', block[end:]):
        return None
    return {'title': _collapse(title), 'author': author}


_DATE_SUFFIX = (r'(?:[a-z]|\{?\\natexlab\s*(?:\{[a-z]\}|[a-z])\}?)?')
_AUTHOR_BLOCK_DATE = re.compile(r'\.\s*(?P<year>(?:19|20)\d{2})' + _DATE_SUFFIX + r'[.,]?\s*$', re.I)
_TITLE_BLOCK_DATE = re.compile(
    r',\s*(?:(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s*)?'
    r'(?P<year>(?:19|20)\d{2})' + _DATE_SUFFIX + r'[.,]?\s*$', re.I)
_NUMERIC_TITLE_DATE = re.compile(r',\s*(?:0?[1-9]|1[0-2])\s+(?P<year>(?:19|20)\d{2})'
                                  + _DATE_SUFFIX + r'[.,]?\s*$', re.I)
_TITLE_VOLUME = re.compile(r',\s*(?:volume|vol\.)\s*~?\s*(?P<volume>\d+)'
                           r'(?:\s+of\s+(?P<series>.+?))?[.]?\s*$', re.I)
_TITLE_PAGES = re.compile(r',\s*(?:pages?|pp?\.)\s+(?P<pages>\d{1,6}\s*(?:-{1,3}|[\u2013\u2014])\s*\d{1,6})[.]?\s*$', re.I)


def _label_publication_year(label: str | None) -> str | None:
    """Read author-year label syntax, never an arbitrary number in a label."""
    if not label:
        return None
    marker = re.search(r'\\citeauthoryear(?![A-Za-z@])', label)
    if marker:
        values, pos = [], marker.end()
        for _ in range(3):
            pos = _skip_ws(label, pos)
            if pos >= len(label) or label[pos] != '{':
                break
            try:
                value, pos = _read_braced(label, pos)
            except ValueError:
                return None
            values.append(value)
        if len(values) in (2, 3):
            date = re.fullmatch(r'((?:19|20)\d{2})' + _DATE_SUFFIX, values[-1].strip(), re.I)
            if date:
                return date[1]
    years = []
    pattern = re.compile(r'\(((?:19|20)\d{2})' + _DATE_SUFFIX + r'\)', re.I)
    for match in pattern.finditer(label):
        if any(c.isalpha() for c in clean_text(label[:match.start()])):
            years.append(match[1])
    return years[0] if years and len(set(years)) == 1 else None


def _explicit_bibinfo_fields(body: str) -> tuple[dict[str, str], list[str]] | None:
    """Read REVTeX's labeled fields without inferring absent title or dates.

    A note is consumed as one bounded group, so nested values or erratum years
    inside it cannot override the reference's explicit publication year.
    """
    marker = re.compile(r'\\bibinfo(?![A-Za-z@])')
    if marker.search(body) is None:
        return None
    fields, warnings, authors, pos = {}, [], [], 0
    author_spans = []
    for container in re.finditer(r'\\bibfield(?![A-Za-z@])', body):
        try:
            kind, end = _read_braced(body, _skip_ws(body, container.end()))
            start = _skip_ws(body, end)
            _, end = _read_braced(body, start)
            if kind.strip().lower() == 'author':
                author_spans.append((start, end))
        except ValueError:
            continue
    supported = {'title', 'year', 'journal', 'booktitle', 'doi', 'url', 'note',
                 'pages', 'volume', 'number', 'publisher', 'edition', 'eprint', 'collaboration',
                 'organization', 'date', 'howpublished', 'address', 'series', 'type',
                 'institution', 'school', 'isbn', 'issn', 'chapter', 'editor', 'numpages', 'eid'}
    while match := marker.search(body, pos):
        try:
            start = _skip_ws(body, match.end())
            if start >= len(body) or body[start] != '{':
                raise ValueError('missing field group')
            name, end = _read_braced(body, start)
            start = _skip_ws(body, end)
            if start >= len(body) or body[start] != '{':
                raise ValueError('missing value group')
            value, pos = _read_braced(body, start)
        except ValueError as exc:
            warnings.append(f'BIBITEM_BIBINFO_MALFORMED: {exc}')
            pos = match.end()
            continue
        name = name.strip().lower()
        if name == 'author' or (name == 'person' and any(start <= match.start() < end for start, end in author_spans)):
            authors.append(value)
            continue
        if name not in supported:
            warnings.append(f'BIBITEM_BIBINFO_UNSUPPORTED_FIELD: {name}')
            continue
        if name in fields and fields[name] != value:
            warnings.append(f'BIBITEM_BIBINFO_CONFLICT field={name}: first kept')
            continue
        fields[name] = value
    if authors:
        fields['author'] = ' and '.join(authors)
    if fields.get('date'):
        source_date = fields.pop('date').strip()
        fields['date_raw'] = source_date
        date_year = None
        if re.fullmatch(r'\d{4}', source_date) and int(source_date) > 0:
            date_year = int(source_date)
        elif re.fullmatch(r'\d{4}-\d{2}-\d{2}', source_date):
            try:
                date_year = calendar_date.fromisoformat(source_date).year
            except ValueError:
                warnings.append('BIBITEM_BIBINFO_DATE_UNRESOLVED: invalid ISO date')
        elif re.search(r'\d{4}', source_date):
            warnings.append('BIBITEM_BIBINFO_DATE_UNRESOLVED: unsupported date syntax')
        if date_year is not None:
            fields['date'] = source_date
            if not fields.get('year'):
                fields['year'] = date_year
    fields['parse_mode'] = 'bibinfo_fields'
    if fields.get('year'):
        fields['year_source'] = 'explicit_bibinfo'
    return fields, warnings


def _single_macro_block(block: str) -> tuple[str, str] | None:
    """Recognize a whole one-argument macro block, not prose containing one."""
    match = re.match(r'\s*\\([A-Za-z@]+)\s*(?=\{)', block)
    if match is None:
        return None
    try:
        value, end = _read_braced(block, match.end())
    except ValueError:
        return None
    if block[end:].strip(' \t\r\n.,;'):
        return None
    return match[1], value


def _validated_author_year_prefix(body: str, year: str) -> tuple[str, str] | None:
    """Read an explicit surname/initial-pair list ending at its label date.

    Every pair must validate; this does not recover full names from initials or
    interpret a free-form prose prefix as people.
    """
    date = re.search(r'(?<![\d.])' + re.escape(year) + _DATE_SUFFIX + r'(?=\s*[,.;])', body)
    if date is None:
        return None
    raw = body[:date.start()].strip()
    cleaned, unknown = _bibitem_author_text(raw)
    if unknown:
        return None
    parts = _split_depth0(cleaned, _COMMA)
    truncated = bool(parts and re.fullmatch(r'et\s*al\.?', clean_text(parts[-1]), re.I))
    if truncated:
        parts.pop()
    if not parts or len(parts) % 2:
        return None
    people = []
    for idx in range(0, len(parts), 2):
        family = re.sub(r'^(?:and\s+|&\s*)', '', clean_text(parts[idx]), flags=re.I)
        initials = clean_text(parts[idx + 1])
        if (not family or any(c.isdigit() for c in family)
                or not any(c.isalpha() for c in family) or not _INITIALS.fullmatch(initials)):
            return None
        people.append(initials + ' ' + family)
    if truncated:
        people.append('et al.')
    return ' and '.join(people), raw


def _bibitem_fields(body: str, label: str | None) -> dict[str, str]:
    fields: dict[str, str] = {"raw": _collapse(body)}
    # These style switches change spacing, not author/reference identity. Keep
    # the untouched body above for provenance; parse only the cleaned copy.
    body = _BIB_SPACING.sub('', body)
    if label is not None:
        fields["label"] = _collapse(label)

    m = _DOI.search(body)
    if m:
        fields["doi"] = m.group(0).rstrip(".)")
    m = _URL.search(body)
    if m:
        url = m.group(1) or m.group(2)
        if url:
            fields["url"] = url.rstrip(".,;)")
    m = _ARXIV.search(body)
    if m:
        fields["eprint"] = m.group(1)

    # Year: last 4-digit year outside URLs / DOIs / arXiv ids / page ranges.
    cleaned = _ARXIV.sub(" ", _DOI.sub(" ", _URL.sub(" ", body)))
    cleaned = _PAGES.sub(" ", cleaned)
    years = _YEAR.findall(cleaned)
    if years:
        fields["year"] = years[-1]
    label_year = _label_publication_year(label)
    if label_year:
        fields['year'] = label_year
        fields['year_source'] = 'author_year_label'

    explicit = _explicit_bibinfo_fields(body)
    if explicit is not None:
        parsed, issues = explicit
        if not label_year:
            fields.pop('year', None)  # an absent explicit year is not the note's year
        fields.update(parsed)
        if issues:
            fields['bibinfo_parse_warnings'] = issues
        return fields

    blocks = [_collapse(b) for b in _NEWBLOCK.split(body)]
    blocks = [b for b in blocks if b]
    if len(blocks) >= 2:
        quote_only = _QUOTED_TITLE.match(blocks[0])
        if quote_only and re.fullmatch(r'\s*,?\s*((?:19|20)\d{2})[.,]?\s*', blocks[0][quote_only.end():]):
            fields['title'] = _collapse(quote_only[1]).strip(' ,.;')
            fields['parse_mode'] = 'quoted_title_only_first_block'
            first_years = _YEAR.findall(blocks[0][quote_only.end():])
            fields['year'] = first_years[-1]
            fields['year_source'] = 'first_block_bibliography_date'
            fields['continuation_raw'] = ' '.join(blocks[1:])
            return fields
        quoted_first = _quoted_title_fields(blocks[0])
        if quoted_first and _usable_explicit_bibitem_author(quoted_first['author']):
            fields.update(quoted_first)
            fields['parse_mode'] = 'quoted_first_block'
            # Received/accepted years and notes in subsequent newblocks are
            # not publication dates of this already complete reference block.
            first = _ARXIV.sub(' ', _DOI.sub(' ', _URL.sub(' ', blocks[0])))
            first_years = _YEAR.findall(_PAGES.sub(' ', first))
            if first_years:
                fields['year'] = first_years[-1]
                fields['year_source'] = 'first_block_bibliography_date'
            elif not label_year:
                fields.pop('year', None)
            fields['continuation_raw'] = ' '.join(blocks[1:])
            return fields
        emphasized_first = _emphasized_first_block_fields(blocks[0])
        if emphasized_first:
            fields.update(emphasized_first)
            fields['parse_mode'] = 'emphasized_first_block'
            fields['continuation_raw'] = ' '.join(blocks[1:])
            return fields
        if len(blocks) == 2 and re.match(r'In\s*\\(?:emph|textit)(?![A-Za-z@])', blocks[1], re.I):
            # No separate title/author block can be identified in this layout;
            # do not mistake the whole work title for a list of people.
            fields['parse_mode'] = 'raw_only'
            return fields
        macro = _single_macro_block(_unwrap_links(blocks[1]))
        if macro and macro[0] not in ('emph', 'textit', 'textbf'):
            fields['parse_mode'] = 'raw_only'
            fields['omitted_title_identifier'] = macro[0]
            if macro[0].casefold() == 'doarxiv' and re.fullmatch(r'\d{4}\.\d{4,5}(?:v\d+)?', macro[1]):
                fields['eprint'] = macro[1]
            elif macro[0] == 'path' and _DOI.search(macro[1]):
                pass  # DOI was already extracted from the literal source above
            elif macro[0] not in ('url', 'dodoi'):
                fields['bibinfo_parse_warnings'] = [f'BIBITEM_TITLE_MACRO_UNSUPPORTED: {macro[0]}']
            return fields
        fields["parse_mode"] = "newblock"
        author_date = _AUTHOR_BLOCK_DATE.search(blocks[0])
        if author_date:
            fields['year'] = author_date['year']
            fields['year_source'] = 'newblock_author_date'
        elif len(blocks) >= 3:
            # Prefer a bibliographic venue date to a year in the title or an
            # arXiv display identifier elsewhere in the entry.
            venue_block = _ARXIV.sub(' ', _DOI.sub(' ', _URL.sub(' ', blocks[2])))
            venue_block = _PAGES.sub(' ', venue_block)
            parenthesized = re.search(r'\(((?:19|20)\d{2})\)', venue_block)
            venue_years = _YEAR.findall(venue_block)
            if parenthesized or venue_years:
                fields['year'] = parenthesized[1] if parenthesized else venue_years[-1]
                fields['year_source'] = 'newblock_venue_date'
        fields["author"] = blocks[0]
        if fields.get('year'):
            author_without_year, removed_year = re.subn(r'(?<=[\s.])' + re.escape(fields['year'])
                                                        + _DATE_SUFFIX + r'[.,]?\s*$', '', fields['author'])
            author_without_year = author_without_year.rstrip()
            if removed_year and author_without_year:
                fields['author_raw'] = fields['author']
                fields['author'] = author_without_year
        fields["title"] = blocks[1]
        title_pages = _TITLE_PAGES.search(fields['title'])
        if title_pages and fields['title'][:title_pages.start()].strip():
            fields['title_raw'] = fields['title']
            fields['title_pages_raw'] = fields['title'][title_pages.start():]
            fields['pages'] = title_pages['pages']
            fields['title'] = fields['title'][:title_pages.start()].rstrip()
        title_volume = _TITLE_VOLUME.search(fields['title'])
        if title_volume and fields['title'][:title_volume.start()].strip():
            fields['title_raw'] = fields['title']
            fields['title_volume_raw'] = fields['title'][title_volume.start():]
            fields['volume'] = title_volume['volume']
            if title_volume['series']:
                fields['series'] = title_volume['series'].rstrip(' .')
            fields['title'] = fields['title'][:title_volume.start()].rstrip()
        title_date = _TITLE_BLOCK_DATE.search(fields['title']) or _NUMERIC_TITLE_DATE.search(fields['title'])
        if title_date and title_date['year'] == fields.get('year'):
            title_without_date = fields['title'][:title_date.start()].rstrip()
            if title_without_date:
                fields.setdefault('title_raw', fields['title'])
                fields['title_date_raw'] = fields['title'][title_date.start():]
                fields['title'] = title_without_date
        if len(blocks) >= 3:
            if _VENUE_HINT.search(blocks[2]):
                fields["booktitle"] = _bibitem_venue(blocks[2])
            else:
                fields["journal"] = _bibitem_venue(blocks[2])
    else:
        quoted = _quoted_title_fields(body)
        if quoted:
            fields.update(quoted)
            fields["parse_mode"] = "quoted"
        else:
            emphasized = _emphasized_title_fields(body)
            if emphasized:
                fields.update(emphasized)
                fields['parse_mode'] = 'emphasized_title'
            else:
                fields["parse_mode"] = "raw_only"
    if fields['parse_mode'] == 'raw_only' and label_year:
        explicit_authors = _validated_author_year_prefix(body, label_year)
        if explicit_authors:
            fields['author'], fields['author_raw'] = explicit_authors
            fields['author_source'] = 'validated_author_year_prefix'
    if fields['parse_mode'] == 'raw_only' and not fields.get('year_source'):
        venue_year = _emphasized_venue_year(body)
        if venue_year:
            fields['year'] = venue_year
            fields['year_source'] = 'emphasized_venue_date'
        elif len(set(years)) > 1:
            fields.pop('year', None)
            fields['year_raw'] = ' | '.join(dict.fromkeys(years))
            fields['bibinfo_parse_warnings'] = ['BIBITEM_YEAR_UNRESOLVED: multiple unscoped year-like numbers']
    return fields


def _bibitem_author_text(author: str) -> tuple[str, list[str]]:
    """Unwrap only known author formatting, preserving unknown macro literals."""
    author = _BIB_SPACING.sub('', author)
    smallcaps = re.match(r'\s*\{\s*\\(?:sc|scshape)\s+', author)
    if smallcaps:
        try:
            wrapped, end = _read_braced(author, author.index('{'))
            if not author[end:].strip(' .\t\r\n'):
                unwrapped = re.sub(r'^\s*\\(?:sc|scshape)\s+', '', wrapped)
                pieces = _split_depth0(unwrapped, _COMMA)
                valid = bool(pieces) and len(pieces) % 2 == 0
                for idx in range(0, len(pieces) - 1, 2):
                    family = re.sub(r'^and\s+', '', clean_text(pieces[idx]), flags=re.I)
                    valid = valid and any(c.isalpha() for c in family) and bool(_INITIALS.fullmatch(clean_text(pieces[idx + 1])))
                if valid:
                    author = unwrapped
        except ValueError:
            pass
    author = _AUTHOR_WRAPPER.sub('', author)
    author = re.sub(r'\\textquoteright(?![A-Za-z@])(?:\{\})?', "'", author)
    def same_literal_letter(match):
        math, text = match['math'].strip(), match['text'].strip().lstrip('~').strip()
        if math == r'\imath' and re.fullmatch(r'\\i(?:\{\})?', text):
            return r'\i{}'
        boxed = _single_macro_block(math)
        if boxed and boxed[0] == 'mbox' and re.fullmatch(r'\\c\s*\{[A-Za-z]\}', text):
            if clean_text(boxed[1]) == clean_text(text):
                return text
        breve = re.fullmatch(r'\\breve\s*\{([A-Za-z])\}', math)
        if breve and (len(text) == 1 or re.fullmatch(r'\\u\s*\{[A-Za-z]\}', text)):
            literal = unicodedata.normalize('NFC', breve[1] + '\u0306')
            if literal == clean_text(text):
                return text
        return match.group()
    author = re.sub(r'\\ifmmode(?P<math>.*?)\\else(?P<text>.*?)\\fi(?:\{\})?',
                    same_literal_letter, author, flags=re.S)
    author = re.sub(r'\\(?:\s|[,;!])', ' ', author)
    author = re.sub(r'\\biband(?![A-Za-z@])', ' and ', author)
    pos = 0
    marker = re.compile(r'\\bibinfo(?![A-Za-z@])')
    while match := marker.search(author, pos):
        try:
            field_start = _skip_ws(author, match.end())
            if field_start >= len(author) or author[field_start] != '{':
                break
            field, end = _read_braced(author, field_start)
            payload_start = _skip_ws(author, end)
            if payload_start >= len(author) or author[payload_start] != '{':
                break
            payload, end = _read_braced(author, payload_start)
            if field.strip().lower() not in ('author', 'editor'):
                pos = end
                continue
            author = author[:match.start()] + payload + author[end:]
            pos = match.start()
        except ValueError:
            break
    plain = clean_text(author)
    unknown = sorted(set(re.findall(r'\\[A-Za-z@]+', plain)))
    return author, unknown


def _usable_explicit_bibitem_author(author: str) -> bool:
    """Exclude unresolved macros, broken groups and non-name placeholders."""
    author, unknown = _bibitem_author_text(author)
    if unknown or _REPEATED_AUTHOR.fullmatch(author):
        return False
    try:
        _, end = _read_braced('{' + author + '}', 0)
        if end != len(author) + 2:
            return False
    except ValueError:
        return False
    names = _authors_from_bibitem(author)
    return bool(names) and all(any(c.isalpha() for c in name) for name in names)


def _read_label(text: str, i: int) -> tuple[str, int] | None:
    """text[i] == '['. Label ends at the first ']' outside any braces."""
    depth, j, n = 0, i + 1, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return None
        elif c == "]" and depth == 0:
            return text[i + 1:j], j + 1
        j += 1
    return None


def _bibitem_head(text: str, m: re.Match) -> tuple[str | None, str, int] | None:
    """Parse the optional [label] and the {key} after \\bibitem."""
    i = _skip_ws(text, m.end())
    label = None
    if i < len(text) and text[i] == "[":
        got = _read_label(text, i)
        if got is None:
            return None
        label, i = got
        i = _skip_ws(text, i)
    if i >= len(text) or text[i] != "{":
        return None
    try:
        key, i = _read_braced(text, i)
    except ValueError:
        return None
    return label, key.strip(), i


def parse_bibitems(text: str) -> tuple[list[BibEntry], list[str]]:
    """Parse \\bibitem entries from thebibliography / .bbl text."""
    text = _strip_comments(text)
    heads: list[tuple[int, int, str | None, str]] = []
    warnings: list[str] = []
    malformed_heads = []
    for m in _BIBITEM_CMD.finditer(text):
        head = _bibitem_head(text, m)
        if head is None:
            warnings.append(f"malformed \\bibitem at offset {m.start()}; skipped")
            malformed_heads.append(m.start())
            continue
        label, key, body_start = head
        heads.append((m.start(), body_start, label, key))

    entries: list[BibEntry] = []
    previous_author = None
    previous_explicit_key = None
    previous_start = 0
    for idx, (head_start, body_start, label, key) in enumerate(heads):
        if (_BIB_CONTEXT.search(text[previous_start:head_start])
                or any(previous_start <= offset < head_start for offset in malformed_heads)):
            previous_author = previous_explicit_key = None
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(text)
        body = text[body_start:end]
        stop = _END_BIB.search(body)
        if stop:
            body = body[:stop.start()]
        if not key:
            warnings.append(f"\\bibitem #{idx + 1} has an empty key; skipped")
            previous_author = previous_explicit_key = None
            previous_start = body_start
            continue
        fields = _bibitem_fields(body, label)
        warnings.extend(f'{message} key={key}' for message in fields.pop('bibinfo_parse_warnings', []))
        author = fields.get('author', '')
        if not author:
            repeated = _REPEATED_PREFIX.match(_BIB_SPACING.sub('', body))
            if repeated:
                author = repeated.group()
        if _REPEATED_AUTHOR.fullmatch(author):
            if previous_author:
                fields['author'] = previous_author
                fields['author_repeated_from'] = previous_explicit_key
            else:
                fields.pop('author', None)
                warnings.append(f'BIBITEM_REPEATED_AUTHOR_UNRESOLVED key={key}: no preceding explicit author in bibliography')
        else:
            cleaned_author, unknown = _bibitem_author_text(author)
            if unknown:
                warnings.append(f'BIBITEM_AUTHOR_MACRO_UNSUPPORTED key={key}: ' + ', '.join(unknown))
            elif len(_split_depth0(cleaned_author, re.compile(';'))) > 1 and not _authors_from_bibitem(cleaned_author):
                warnings.append(f'BIBITEM_AUTHOR_STYLE_UNSUPPORTED key={key}: malformed semicolon name list')
            if author:
                fields['author'] = cleaned_author
            if _usable_explicit_bibitem_author(cleaned_author):
                previous_author, previous_explicit_key = cleaned_author, key
            else:
                previous_author = previous_explicit_key = None
        entries.append(BibEntry(original_key=key, entry_type="misc",
                                fields=fields))
        previous_start = body_start
    return entries, warnings


def _compatible_bbl_authors(previous: BibEntry, fallback: BibEntry) -> bool:
    """Accept formatted initials only with independent strong work identity.

    This never replaces native authors with abbreviated names. Token count and
    order stay strict; only an explicitly truncated fallback may match a prefix.
    """
    left, right = previous.fields, fallback.fields
    same_doi = bool(_identity_doi(left) and _identity_doi(left) == _identity_doi(right))
    matching_title = bool(left.get('title') and right.get('title')
                          and _alnum(left['title']) == _alnum(right['title']))
    same_title_year = bool(matching_title and left.get('year') is not None
                           and left.get('year') == right.get('year'))
    long_title_missing_year = bool(matching_title and len(_title_tokens(left['title'])) >= 5
        and (left.get('year') is None or right.get('year') is None))
    a_id, b_id = _identity_arxiv(left), _identity_arxiv(right)
    same_arxiv = bool(a_id and a_id == b_id)
    if not (same_doi or same_arxiv or same_title_year or long_title_missing_year):
        return False
    native, printed = left.get('authors', []), right.get('authors', [])
    if not native or not printed:
        return False
    if len(native) != len(printed):
        if not right.get('authors_truncated') or len(printed) > len(native):
            return False
    for full, abbreviated in zip(native, printed):
        # Apostrophes inside a name are not an extra given-name component:
        # Shin'ichiro -> S., O'Sullivan stays one family-name token.
        token = r"[^\W_]+(?:['\u2019][^\W_]+)*"
        a = [_author_key(x) for x in re.findall(token, _fold(full), flags=re.UNICODE)]
        b = [_author_key(x) for x in re.findall(token, _fold(abbreviated), flags=re.UNICODE)]
        if not a or not b or a[-1] != b[-1] or len(a) != len(b):
            return False
        for x, y in zip(a[:-1], b[:-1]):
            if x != y and not (len(x) == 1 and y.startswith(x)) and not (len(y) == 1 and x.startswith(y)):
                return False
    return True


def _identifier_url(value: object) -> tuple[str, str] | None:
    """Recognize identifier links by their actual host, not a substring."""
    try:
        url = urlsplit(str(value or ''))
    except ValueError:
        return None
    if url.scheme.lower() not in {'http', 'https'}:
        return None
    path = unquote(url.path).strip('/')
    if url.hostname in {'doi.org', 'dx.doi.org', 'www.doi.org'} and path.startswith('10.'):
        return ('doi', path.casefold())
    if url.hostname in {'arxiv.org', 'www.arxiv.org', 'export.arxiv.org'}:
        match = re.fullmatch(r'(?:abs|pdf)/(.+?)(?:\.pdf)?', path, re.I)
        if match and _ARXIV_ID.fullmatch(match[1]):
            return ('arxiv', re.sub(r'v\d+$', '', match[1], flags=re.I).casefold())
    return None


def _identity_doi(fields: dict) -> str | None:
    return fields.get('doi') if fields.get('doi_source') != 'note' else None


def _identity_arxiv(fields: dict) -> str | None:
    explicit = re.sub(r'^arxiv\s*:\s*', '', str(fields.get('eprint') or '').strip(), flags=re.I)
    if _ARXIV_ID.fullmatch(explicit):
        return re.sub(r'v\d+$', '', explicit, flags=re.I).casefold()
    identifier = _identifier_url(fields.get('url'))
    if identifier and identifier[0] == 'arxiv':
        return identifier[1]
    # A literal arXiv: identifier in the journal metadata is explicit evidence,
    # unlike an arbitrary host containing the substring 'arxiv.org'.
    literal = _ARXIV.search(str(fields.get('journal') or ''))
    return literal[1].casefold() if literal else None


def _author_key(value: str) -> str:
    return ''.join(c for c in _fold(value) if c.isalnum())


def _entry_identity(left: BibEntry, right: BibEntry) -> str | None:
    a, b = left.fields, right.fields
    a_doi, b_doi = _identity_doi(a), _identity_doi(b)
    if a_doi and b_doi:
        if a_doi != b_doi:
            return None
        return 'doi'
    a_id, b_id = _identity_arxiv(a), _identity_arxiv(b)
    if a_id and b_id:
        return 'arxiv_id' if a_id == b_id else None
    same_title_year = bool(a.get('title') and b.get('title')
        and len(_title_tokens(a['title'])) >= 3
        and _alnum(a['title']) == _alnum(b['title'])
        and a.get('year') is not None and a['year'] == b.get('year'))
    authors_match = bool(a.get('authors') and b.get('authors') and (
        [_author_key(x) for x in a['authors']] == [_author_key(x) for x in b['authors']]
        or _compatible_bbl_authors(left, right)))
    return 'title_year_authors' if same_title_year and authors_match else None


def _compare_entries(previous: BibEntry, fallback: BibEntry) -> dict:
    """Field decisions for one source-local citation key; never overwrite native values."""
    identity = _entry_identity(previous, fallback)
    conflicts, equivalents, alternates = [], [], []
    a, b = previous.fields, fallback.fields
    for name in _CANON_FIELDS:
        left, right = a.get(name), b.get(name)
        if not left or not right:
            continue
        if name in {'title', 'journal'}:
            same = _alnum(left) == _alnum(right)
        elif name == 'authors':
            same = ([_author_key(x) for x in left] == [_author_key(x) for x in right]
                    or _compatible_bbl_authors(previous, fallback))
        elif name == 'url':
            same = left == right
            l_id, r_id = _identifier_url(left), _identifier_url(right)
            if not same and l_id and l_id == r_id:
                same = True
                if left != right:
                    alternates.append(right)
            # A DOI landing page and a publisher/repository page can both be
            # source-provided links for the same explicit DOI. Preserve both;
            # never generalize this to unrelated/contradictory identifier URLs.
            contradictory = bool(l_id and r_id and l_id[0] == r_id[0] and l_id != r_id)
            doi_link = ('doi', str(a.get('doi', '')).casefold())
            if not same and identity == 'doi' and not contradictory and doi_link in (l_id, r_id):
                try:
                    valid = all(urlsplit(v).scheme in {'http', 'https'} and urlsplit(v).hostname
                                for v in (left, right))
                except ValueError:
                    valid = False
                if valid:
                    same = True
                    alternates.append(right)
        else:
            same = left == right
        (equivalents if same else conflicts).append(name)
    # Conflicting explicit identifiers veto even otherwise plausible enrichment.
    a_doi, b_doi = _identity_doi(a), _identity_doi(b)
    identifiers_conflict = bool(a_doi and b_doi and a_doi != b_doi)
    a_id, b_id = _identity_arxiv(a), _identity_arxiv(b)
    identifiers_conflict |= bool(a_id and b_id and a_id != b_id)
    if a_id and b_id and a_id != b_id:
        conflicts.append('arxiv_id')
    if identifiers_conflict:
        identity = None
    allowed = not conflicts or bool(identity)
    if identifiers_conflict:
        allowed = False
    fills = [name for name in _CANON_FIELDS if not a.get(name) and b.get(name)] if allowed else []
    return dict(identity=identity, conflicting_fields=conflicts, equivalent_fields=equivalents,
                filled_fields=fills, alternative_urls=alternates, allow_enrichment=allowed,
                native_identifiers={'doi': a_doi, 'arxiv_id': a_id},
                fallback_identifiers={'doi': b_doi, 'arxiv_id': b_id})


def extract_bibliography_detailed(document: GatheredDocument, *,
                                  conflict_sink: list[dict] | None = None) -> tuple[list[BibEntry], list[str]]:
    """Extract raw entries of one version: .bib, then .bbl, then inline \\bibitem."""
    tag = f"[{document.arxiv_id} v{document.version}]"
    sources: list[tuple[str, list[BibEntry], list[str]]] = []
    # Native databases precede derived BBL conversions, regardless of basename.
    paths = sorted(document.bib_files, key=lambda p: (p.lower().endswith('.bbl.bib'), p))
    for path in [p for p in paths if not p.lower().endswith((".bbl", ".bbl.tex"))]:
        sources.append((path, *parse_bib(document.bib_files[path])))
    for path in [p for p in paths if p.lower().endswith((".bbl", ".bbl.tex"))]:
        raw = document.bib_files[path]
        if is_biblatex_bbl(raw):
            parsed, warns = parse_biblatex_bbl(raw)
        elif is_amsrefs_bbl(raw):
            parsed, warns = parse_amsrefs_bbl(raw)
        else:
            parsed, warns = parse_bibitems(raw)
        if not parsed and raw.strip() and not warns and not is_empty_bibliography(raw):
            warns.append("BBL_NO_BIBITEM: unsupported or empty bibliography format")
        sources.append((path, parsed, warns))
    sources.append(("<latex>", *parse_bibitems(document.latex)))
    if is_amsrefs_bbl(document.latex):
        sources.append(("<latex>", *parse_amsrefs_bbl(document.latex)))

    entries: list[BibEntry] = []
    diagnostics: list[str] = []
    seen: set[str] = set()
    by_key: dict[str, int] = {}
    origins: dict[str, str] = {}
    for path, parsed, warns in sources:
        for w in warns:
            log.warning("%s stage=bib_parse file=%s: %s", tag, path, w)
            diagnostics.append(f'{tag} stage=bib_parse file={path}: {w}')
        for e in parsed:
            fold = e.original_key.casefold()  # BibTeX keys are case-insensitive
            if fold and fold in seen:
                conflicts = []
                decision = None
                if (path.lower().endswith(('.bbl', '.bbl.bib', '.bbl.tex')) or path == '<latex>'):
                    previous = normalize_entry(entries[by_key[fold]])
                    fallback = normalize_entry(e)
                    decision = _compare_entries(previous, fallback)
                    conflicts = decision['conflicting_fields']
                    if conflict_sink is not None:
                        conflict_sink.append(dict(arxiv_id=document.arxiv_id, version=document.version,
                            citation_key=e.original_key, native_source=origins[fold],
                            fallback_source=path, **decision,
                            native={name: previous.fields.get(name) for name in _CANON_FIELDS},
                            fallback={name: fallback.fields.get(name) for name in _CANON_FIELDS},
                            fallback_parse_mode=fallback.fields.get('parse_mode'),
                            fallback_raw=fallback.fields.get('raw')))
                    if decision['allow_enrichment']:
                        merged = dict(previous.fields)
                        for name, value in fallback.fields.items():
                            # These identify the bibitem parser's input format;
                            # copying them would reinterpret an existing BibTeX
                            # author/title during the final normalization pass.
                            if name in ('raw', 'label', 'parse_mode', 'author'):
                                continue
                            if name == 'authors_truncated' and previous.fields.get('authors'):
                                continue
                            if not merged.get(name) and value:
                                merged[name] = value
                        entries[by_key[fold]] = BibEntry(previous.original_key, previous.entry_type, merged)
                        if decision['alternative_urls']:
                            merged['alternative_urls'] = list(dict.fromkeys(
                                list(previous.fields.get('alternative_urls', [])) + decision['alternative_urls']))
                        if not conflicts:
                            continue
                action = ('native values kept; missing fields enriched'
                          if decision and decision['allow_enrichment'] else 'skipped (first kept)')
                log.warning("%s stage=bib_parse file=%s: duplicate key '%s' %s; conflicts=%s",
                            tag, path, e.original_key, action, ','.join(conflicts))
                diagnostics.append(f'{tag} stage=bib_parse file={path}: duplicate key {e.original_key!r} {action}'
                                   + (f'; conflicting_fields={",".join(conflicts)}' if conflicts else ''))
                continue
            if fold:
                seen.add(fold)
                by_key[fold] = len(entries)
                origins[fold] = path
            entries.append(e)
    entries, crossref_warnings = resolve_crossrefs(entries)
    for warning in crossref_warnings:
        log.warning('%s stage=bib_parse: %s', tag, warning)
        diagnostics.append(f'{tag} stage=bib_parse: {warning}')
    return [normalize_entry(e) for e in entries], diagnostics


def extract_bibliography(document: GatheredDocument) -> list[BibEntry]:
    """Contract interface; diagnostics are available via the detailed helper."""
    return extract_bibliography_detailed(document)[0]


_CITED = re.compile(r'\\(?P<command>[A-Za-z]*[Cc]ite[A-Za-z]*)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{(?P<keys>[^{}]*)\}')


def cited_keys(text: str) -> set[str]:
    """Citation keys as written; no guessing or DOI/S2 lookup."""
    return {key.strip() for match in _CITED.finditer(text)
            if match['command'].casefold() != 'bstctlcite'
            for key in match['keys'].split(',') if key.strip()
            and not (match['command'].casefold() == 'nocite' and key.strip() == '*')}


def build_key_resolver(mapping: dict[str, str]) -> Callable[[str], str | None]:
    """Return a lookup for \\cite keys against a version's key_mapping.

    Exact match first, then case-insensitive (BibTeX semantics). Returns None
    when the key is unknown or the case-insensitive match is ambiguous.
    """
    folded: dict[str, set[str]] = {}
    for old, canon in mapping.items():
        folded.setdefault(old.casefold(), set()).add(canon)

    def resolve(key: str) -> str | None:
        key = key.strip()
        if key in mapping:
            return mapping[key]
        hits = folded.get(key.casefold(), set())
        return next(iter(hits)) if len(hits) == 1 else None

    return resolve


# --------------------------------------------------------------------------
# Field normalization
# --------------------------------------------------------------------------
_ACCENTS = {
    "`": "\u0300", "'": "\u0301", "^": "\u0302", "~": "\u0303", "=": "\u0304",
    "u": "\u0306", ".": "\u0307", '"': "\u0308", "r": "\u030A", "H": "\u030B",
    "v": "\u030C", "c": "\u0327", "k": "\u0328",
}
_SPECIAL = {
    "ss": "ß", "o": "ø", "O": "Ø", "ae": "æ", "AE": "Æ", "oe": "œ",
    "OE": "Œ", "l": "ł", "L": "Ł", "aa": "å", "AA": "Å", "i": "i", "j": "j",
}
_ACC_SYM = re.compile(r"""\\(['`^~=."])\s*(?:\{\\?([A-Za-z])\}|\\?([A-Za-z]))""")
_ACC_LET = re.compile(r"\\([urHvck])(?:\s*\{\\?([A-Za-z])\}|\s+([A-Za-z]))")
_SPECIAL_RE = re.compile(
    r"\\(ss|oe|OE|ae|AE|aa|AA|o|O|l|L|i|j)(?![A-Za-z])(?:\{\}|\s)?"
)
_FMT_CMD = re.compile(
    r"\\(?:emph|textit|textbf|textrm|textsc|textsf|texttt|textsl|mbox|text|"
    r"url|bf|it|em|sc|rm)(?![A-Za-z])"
)
_MATH_SPLIT = re.compile(r"((?<!\\)\$[^$]*(?<!\\)\$)")
_EMPH = re.compile(r"\\(?:emph|textit|textsl)\s*\{([^{}]*)\}")
_YEAR_ANY = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)")
_AND = re.compile(r"\s+and\s+", re.I)
_INITIALS = re.compile(r"^(?:[A-Z]\.?[\s\-]*)+$")
_TYPE_ALIAS = {"conference": "inproceedings"}


def _accent_sub(m: re.Match) -> str:
    base = m.group(2) or m.group(3)
    return unicodedata.normalize("NFC", base + _ACCENTS[m.group(1)])


def clean_text(s: object) -> str:
    """LaTeX markup -> plain text. Inline math ($...$) is preserved."""
    if s is None:
        return ""
    out = []
    for idx, part in enumerate(_MATH_SPLIT.split(_unwrap_links(str(s)))):
        if idx % 2 == 1:
            out.append(part)
            continue
        part = _unwrap_links(part)
        part = re.sub(r'\\penalty\s*-?\d+\s*', '', part)
        part = _ACC_SYM.sub(_accent_sub, part)
        part = _ACC_LET.sub(_accent_sub, part)
        part = _SPECIAL_RE.sub(lambda m: _SPECIAL[m.group(1)], part)
        part = _FMT_CMD.sub("", part)
        for esc, ch in ((r"\&", "&"), (r"\%", "%"), (r"\_", "_"),
                        (r"\#", "#"), (r"\$", "$")):
            part = part.replace(esc, ch)
        part = (part.replace("---", "\u2014").replace("--", "\u2013")
                .replace("~", " ").replace("``", "\u201c").replace("''", "\u201d"))
        out.append(re.sub(r"[{}]", "", part))
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _split_depth0(s: str, sep: re.Pattern) -> list[str]:
    parts, depth, start, i = [], 0, 0, 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            m = sep.match(s, i)
            if m:
                parts.append(s[start:i])
                i = start = m.end()
                continue
        i += 1
    parts.append(s[start:])
    return [p.strip() for p in parts if p.strip()]


_COMMA = re.compile(r",")


def _format_name(name: str) -> str | None:
    pieces = _split_depth0(name, _COMMA)
    if len(pieces) == 2:
        text = f"{pieces[1]} {pieces[0]}"
    elif len(pieces) == 3:
        text = f"{pieces[2]} {pieces[0]} {pieces[1]}"
    else:
        text = name
    return clean_text(text) or None


def _authors_from_bibtex(s: str) -> list[str]:
    # Apply only proven literal formatting before generic brace cleanup would
    # collapse command boundaries (textquoteright{}Sullivan -> ...Sullivan).
    # Unknown/differing conditional branches remain literal, never guessed.
    if r'\ifmmode' in s or r'\textquoteright' in s:
        s, _ = _bibitem_author_text(s)
    names = (_format_name(p) for p in _split_depth0(s, _AND))
    return [n for n in names if n]


_VANCOUVER = re.compile(r"^(\S.*?)\s+([A-Z]{1,3})\.?$")
_ET_AL = re.compile(r"(?:^|[\s,])et\s*al\.?$", re.I)


def _strip_et_al(authors: list[str]) -> tuple[list[str], bool]:
    """Drop 'others' / 'et al.' markers. Returns (authors, was_truncated)."""
    out: list[str] = []
    truncated = False
    for a in authors:
        a = a.strip()
        if a.casefold().rstrip(".") in {"others", "et al", "etal"} or re.fullmatch(r'\d+\s+others\.?', a, re.I):
            truncated = True
            continue
        m = _ET_AL.search(a)
        if m is None:
            m = re.search(r'[\s,]+\d+\s+others\.?$', a, re.I)
        if m:
            a = a[:m.start()].rstrip(" ,")
            truncated = True
        if a:
            out.append(a)
    return out, truncated


def _authors_from_bibitem(s: str) -> list[str]:
    s, unknown = _bibitem_author_text(s)
    if unknown or _REPEATED_AUTHOR.fullmatch(s):
        return []
    s = re.sub(r"(?<![A-Z])\.\s*$", "", s.strip())
    s = re.sub(r",\s+and\s+", " and ", s)
    semicolon_parts = _split_depth0(s, re.compile(';'))
    if len(semicolon_parts) > 1:
        # AAAI-style lists explicitly separate family/initial pairs by ';'.
        # Validate every pair before accepting the list; do not feed fragments
        # containing semicolons through the generic comma splitter.
        people = []
        for segment in semicolon_parts:
            segment = re.sub(r'^\s*and\s+', '', segment, flags=re.I)
            if re.fullmatch(r'et\s*al\.?', clean_text(segment), re.I):
                people.append('et al.')
                continue
            parts = _split_depth0(segment, _COMMA)
            if len(parts) != 2 or not _INITIALS.fullmatch(clean_text(parts[1])):
                return []
            family = clean_text(parts[0])
            if not any(c.isalpha() for c in family):
                return []
            people.append(clean_text(parts[1]) + ' ' + family)
        return people
    result: list[str] = []
    for chunk in _split_depth0(s, _AND):
        plains = [clean_text(t) for t in _split_depth0(chunk, _COMMA)]
        plains = [p for p in plains if p]
        # Vancouver style "Smith J, Doe AB": only when every author matches.
        if len(plains) >= 2 and all(_VANCOUVER.match(p) for p in plains):
            for p in plains:
                m = _VANCOUVER.match(p)
                initials = " ".join(f"{c}." for c in m.group(2))
                result.append(f"{initials} {m.group(1)}")
            continue
        merged: list[str] = []
        for plain in plains:
            et_al = _ET_AL.search(plain)
            if merged and et_al and _INITIALS.fullmatch(plain[:et_al.start()].rstrip(' ,')):
                merged[-1] = plain[:et_al.start()].rstrip(' ,') + ' ' + merged[-1]
                merged.append('et al.')
                continue
            if merged and _INITIALS.match(plain):
                merged[-1] = f"{plain} {merged[-1]}"
            else:
                merged.append(plain)
        result.extend(merged)
    return result


def _year(fields: dict) -> int | None:
    for key in ("year", "date"):
        v = fields.get(key)
        if isinstance(v, int):
            return v
        m = _YEAR_ANY.search(clean_text(v))
        if m:
            return int(m.group(1))
    return None


def _doi(fields: dict) -> str | None:
    for src in (fields.get("doi"), fields.get("url")):
        if not src:
            continue
        m = _DOI.search(str(src).replace(r"\_", "_").replace(r"\%", "%"))
        if m:
            return m.group(0).rstrip(".,;)").lower()
    return None


def _url(fields: dict) -> str | None:
    v = fields.get("url") or ""
    if not v:
        hp = fields.get("howpublished") or ""
        if re.search(r"\\url|https?://", hp):
            v = hp
    if not v:
        return None
    m = re.search(r"\\(?:url|href)\s*\{([^{}]*)\}", v)
    u = m.group(1) if m else v
    for esc, ch in ((r"\_", "_"), (r"\%", "%"), (r"\#", "#"), (r"\&", "&")):
        u = u.replace(esc, ch)
    m2 = re.search(r"(?:https?|ftp)://[^\s{}]+", u)
    if not m2:
        return None
    result = m2.group(0).rstrip(".,;)")
    return re.sub(r'^https?://(?:dx\.)?doi\.org/(?:https?://(?:dx\.)?doi\.org/)+(?=10\.)',
                  'https://doi.org/', result, flags=re.I)


_JOURNAL_MACROS = {
    "aj": "The Astronomical Journal", "actaa": "Acta Astronomica",
    "araa": "Annual Review of Astronomy and Astrophysics",
    "apj": "The Astrophysical Journal", "apjl": "The Astrophysical Journal Letters",
    "apjs": "The Astrophysical Journal Supplement Series", "ao": "Applied Optics",
    "apss": "Astrophysics and Space Science", "aap": "Astronomy and Astrophysics",
    "aapr": "The Astronomy and Astrophysics Review",
    "aaps": "Astronomy and Astrophysics Supplement Series",
    "azh": "Astronomicheskii Zhurnal",
    "baas": "Bulletin of the American Astronomical Society",
    "caa": "Chinese Astronomy and Astrophysics",
    "cjaa": "Chinese Journal of Astronomy and Astrophysics", "icarus": "Icarus",
    "jcap": "Journal of Cosmology and Astroparticle Physics",
    "jrasc": "Journal of the Royal Astronomical Society of Canada",
    "memras": "Memoirs of the Royal Astronomical Society",
    "mnras": "Monthly Notices of the Royal Astronomical Society",
    "na": "New Astronomy", "nar": "New Astronomy Review",
    "pra": "Physical Review A", "prb": "Physical Review B",
    "prc": "Physical Review C", "prd": "Physical Review D",
    "pre": "Physical Review E", "prl": "Physical Review Letters",
    "pasa": "Publications of the Astronomical Society of Australia",
    "pasp": "Publications of the Astronomical Society of the Pacific",
    "pasj": "Publications of the Astronomical Society of Japan",
    "qjras": "Quarterly Journal of the Royal Astronomical Society",
    "rmxaa": "Revista Mexicana de Astronomia y Astrofisica",
    "skytel": "Sky and Telescope", "solphys": "Solar Physics",
    "sovast": "Soviet Astronomy", "ssr": "Space Science Reviews",
    "zap": "Zeitschrift fuer Astrophysik", "nat": "Nature",
    "iaucirc": "International Astronomical Union Circular",
    "aplett": "Astrophysics Letters", "apspr": "Astrophysics Space Physics Research",
    "bain": "Bulletin Astronomical Institute of the Netherlands",
    "fcp": "Fundamental Cosmic Physics", "gca": "Geochimica Cosmochimica Acta",
    "grl": "Geophysical Research Letters", "jcp": "Journal of Chemical Physics",
    "jgr": "Journal of Geophysical Research",
    "jqsrt": "Journal of Quantitative Spectroscopy and Radiative Transfer",
    "memsai": "Memorie della Societa Astronomica Italiana",
    "nphysa": "Nuclear Physics A", "physrep": "Physics Reports",
    "physscr": "Physica Scripta", "planss": "Planetary and Space Science",
    "procspie": "Proceedings of the SPIE",
    "aas": "American Astronomical Society Meeting Abstracts",
    "psj": "The Planetary Science Journal",
    "maps": "Meteoritics and Planetary Science",
    "jaavso": "Journal of the American Association of Variable Star Observers",
    # aliases of macros above
    "apjlett": "The Astrophysical Journal Letters",
    "apjsupp": "The Astrophysical Journal Supplement Series",
    "astap": "Astronomy and Astrophysics",
    "applopt": "Applied Optics",
}
_JOURNAL_MACRO = re.compile(r"^\\([A-Za-z]+)\.?$")
_NOTE_DOI = re.compile(
    r"(?:\bdoi\s*:\s*|doi\.org/|\\doi\s*\{)\s*(10\.\d{4,9}/[^\s,;{}\]\"<>]+)", re.I)


def _expand_journal(journal: str) -> str:
    m = _JOURNAL_MACRO.match(journal.strip())
    return _JOURNAL_MACROS.get(m.group(1).lower(), journal) if m else journal


def _doi_from_note(fields: dict) -> str | None:
    """DOI mentioned in note/howpublished; only with an explicit DOI prefix."""
    for key in ("note", "howpublished"):
        m = _NOTE_DOI.search(str(fields.get(key) or ""))
        if m:
            return m.group(1).rstrip(".,;)").lower()
    return None


def normalize_entry(entry: BibEntry) -> BibEntry:
    """Return a new BibEntry whose fields carry canonical title/authors/year/
    doi/journal/url. All original fields are kept."""
    f = dict(entry.fields)
    from_bibitem = "raw" in f

    title = clean_text(f.get("title"))
    if from_bibitem:
        title = title.rstrip(".").strip()

    raw_auth = f.get("author") or ""
    if isinstance(f.get("authors"), list) and not raw_auth:
        authors = list(f["authors"])
    elif from_bibitem:
        authors = _authors_from_bibitem(raw_auth)
    else:
        authors = _authors_from_bibtex(raw_auth)
    authors, truncated = _strip_et_al(authors)

    jr = f.get("journal") or f.get("journaltitle") or ""
    if from_bibitem:
        m = _EMPH.search(jr)
        jr = m.group(1) if m else jr
    journal_text = clean_text(jr)
    journal = _expand_journal(journal_text) or None
    macro = _JOURNAL_MACRO.match(journal_text.strip())
    if macro:
        f["journal_macro"] = macro.group(1).lower()  # expanded or unknown

    year = _year(f)
    if year is None:
        raw_year = clean_text(f.get("year") or f.get("date"))
        if raw_year:
            f["year_raw"] = raw_year  # present in the source but not a year
    doi = _doi(f)
    if doi is None:
        doi = _doi_from_note(f)
        if doi:
            f["doi_source"] = "note"

    f.update(
        title=title or None,
        authors=authors,
        year=year,
        doi=doi,
        journal=journal,
        url=_url(f),
    )
    if truncated:
        f["authors_truncated"] = True
    if isinstance(f.get("booktitle"), str):
        f["booktitle"] = clean_text(f["booktitle"]) or None
    etype = entry.entry_type.strip().lower()
    return BibEntry(
        original_key=entry.original_key,
        entry_type=_TYPE_ALIAS.get(etype, etype),
        fields=f,
    )


# --------------------------------------------------------------------------
# Cross-version unification
# --------------------------------------------------------------------------
_ARXIV_URL = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Za-z]{2})?/\d{7})",
    re.I,
)
_ARXIV_ID = re.compile(
    r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?", re.I
)
_CANON_FIELDS = ("title", "authors", "year", "doi", "journal", "url")
_QUIET_FIELDS = {"raw", "label"}


class _Sig(NamedTuple):
    doi: str | None
    eprint: str | None
    title: str
    author: str
    year: int | None
    raw: str
    authors: tuple[str, ...]
    words: int


def _fold(s: object) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def _alnum(s: object) -> str:
    return re.sub(r"[^0-9a-z]+", "", _fold(s))


def _title_tokens(title: object) -> set[str]:
    return set(re.findall(r"[0-9a-z]+", _fold(title)))


def _eprint(f: dict) -> str | None:
    v = re.sub(r"^arxiv\s*:\s*", "", str(f.get("eprint") or "").strip(), flags=re.I)
    m = _ARXIV_ID.fullmatch(v)
    if m:
        return m.group(1).lower()
    for src in (f.get("url"), f.get("journal")):
        s = str(src or "")
        m = _ARXIV_URL.search(s) or _ARXIV.search(s)
        if m:
            return m.group(1).lower()
    return None


def _signature(f: dict) -> _Sig:
    authors = f.get("authors") or []
    last = authors[0].split()[-1] if authors and authors[0].split() else ""
    doi = f.get("doi") or None
    if f.get("doi_source") == "note":
        doi = None  # weak evidence: never a merge key
    return _Sig(
        doi=doi,
        eprint=_eprint(f),
        title=_alnum(f.get("title")),
        author=_alnum(last),
        year=f.get("year") if isinstance(f.get("year"), int) else None,
        raw=_alnum(clean_text(_NEWBLOCK.sub(" ", str(f.get("raw") or "")))),
        authors=tuple(_alnum(a) for a in authors),
        words=len(_title_tokens(f.get("title"))),
    )


def _index_keys(sig: _Sig) -> list[tuple[str, object]]:
    keys: list[tuple[str, object]] = []
    if sig.doi:
        keys.append(("doi", sig.doi))
    if sig.eprint:
        keys.append(("eprint", sig.eprint))
    if sig.title:
        keys.append(("title", sig.title))
    if sig.raw:
        keys.append(("raw", sig.raw))
    return keys


def _match(a: _Sig, b: _Sig) -> bool:
    if a.doi and b.doi:
        return a.doi == b.doi
    if a.eprint and b.eprint:
        return a.eprint == b.eprint
    if a.title and b.title:
        if a.title != b.title:
            return False
        if a.author and b.author and a.author != b.author:
            return False
        if a.year and b.year:
            return a.year == b.year
        # A year is missing on one side: the title alone is weak evidence, so
        # require identical full author lists and a title of >= 3 words.
        return bool(a.authors) and a.authors == b.authors and min(a.words, b.words) >= 3
    return bool(a.raw and b.raw and a.raw == b.raw)


def _is_empty(v: object) -> bool:
    return v is None or v == "" or v == []


def _fallback_key(fields: dict) -> str:
    authors = fields.get("authors") or []
    last = _alnum(authors[0].split()[-1]) if authors and authors[0].split() else ""
    year = str(fields["year"]) if fields.get("year") else ""
    m = re.search(r"[0-9a-z]+", _fold(fields.get("title")))
    return f"{last}{year}{m.group(0) if m else ''}" or "ref"


def _merge_cluster(members: list, warnings: list[str], tag: str) -> tuple[str, dict]:
    fields: dict[str, object] = {}
    etype = "misc"
    has_own_doi = any(e.fields.get("doi") and e.fields.get("doi_source") != "note"
                      for _, e, _ in members)
    for ver, entry, _ in members:
        if entry.entry_type and entry.entry_type != "misc":
            etype = entry.entry_type
        for name, value in entry.fields.items():
            if _is_empty(value):
                continue
            if (has_own_doi and name in ("doi", "doi_source")
                    and entry.fields.get("doi_source") == "note"):
                continue  # a DOI found in a note never overrides an own DOI
            value = list(value) if isinstance(value, list) else value
            if name in fields and fields[name] != value and name in _CANON_FIELDS:
                warnings.append(
                    f"{tag} stage=bib_unify v{ver}: conflicting '{name}' for key "
                    f"'{entry.original_key}'; kept value of the latest version")
            fields[name] = value
    if fields.get("year") is not None:
        fields.pop("year_raw", None)
    for name, default in (("title", None), ("authors", []), ("year", None),
                          ("doi", None), ("journal", None), ("url", None)):
        fields.setdefault(name, default)
    source_keys: dict[int, list[str]] = {}
    for ver, entry, _ in members:
        keys = source_keys.setdefault(ver, [])
        if entry.original_key and entry.original_key not in keys:
            keys.append(entry.original_key)
    fields["source_keys"] = source_keys
    return etype, fields


def _possible_duplicates(tag: str, entries: list[BibEntry]) -> list[str]:
    """Report (never merge) entries that look like the same work."""
    buckets: dict[tuple[str, int], list[tuple[str, set[str]]]] = {}
    for e in entries:
        f = e.fields
        authors, year = f.get("authors") or [], f.get("year")
        tokens = _title_tokens(f.get("title"))
        if not authors or not isinstance(year, int) or len(tokens) < 2:
            continue
        parts = authors[0].split()
        surname = _alnum(parts[-1]) if parts else ""
        if surname:
            buckets.setdefault((surname, year), []).append((e.original_key, tokens))
    out: list[str] = []
    for _, items in sorted(buckets.items()):
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (k1, t1), (k2, t2) = items[i], items[j]
                if len(t1 & t2) / min(len(t1), len(t2)) >= 0.8:
                    out.append(f"{tag} stage=bib_unify: possible duplicate "
                               f"(not merged): '{k1}' ~ '{k2}'")
    return out


def unify_bibliography(
    arxiv_id: str, entries_by_version: dict[int, list[BibEntry]],
) -> BibliographyResult:
    """Merge the bibliographies of all versions of one publication."""
    tag = f"[{arxiv_id}]"
    warnings: list[str] = []
    versions = sorted(entries_by_version)

    clusters: list[list[tuple[int, BibEntry, _Sig]]] = []
    index: dict[tuple[str, object], list[int]] = {}
    for ver in versions:
        for raw_entry in entries_by_version[ver]:
            entry = normalize_entry(raw_entry)
            sig = _signature(entry.fields)
            candidates = sorted({ci for k in _index_keys(sig) for ci in index.get(k, [])})
            target = None
            for ci in candidates:
                members = clusters[ci]
                if any(sig.doi and m[2].doi and sig.doi != m[2].doi for m in members):
                    continue
                if any(_match(sig, m[2]) for m in members):
                    target = ci
                    break
            if target is None:
                target = len(clusters)
                clusters.append([])
            clusters[target].append((ver, entry, sig))
            for k in _index_keys(sig):
                ids = index.setdefault(k, [])
                if target not in ids:
                    ids.append(target)

    merged = [_merge_cluster(c, warnings, tag) for c in clusters]

    used: set[str] = set()
    canon_keys: list[str] = []
    for members, (_, fields) in zip(clusters, merged):
        candidates: list[str] = []
        for _, e, _ in members:
            if e.original_key and e.original_key not in candidates:
                candidates.append(e.original_key)
        key = next((k for k in candidates if k.casefold() not in used), None)
        if key is None:
            base = candidates[0] if candidates else _fallback_key(fields)
            key, n = base, 2
            while key.casefold() in used:
                key = f"{base}_{n}"
                n += 1
        if candidates and key != candidates[0]:
            warnings.append(
                f"{tag} stage=bib_unify: key '{candidates[0]}' already used by a "
                f"different entry; canonical key '{key}' assigned")
        used.add(key.casefold())
        canon_keys.append(key)

    mapping: dict[int, dict[str, str]] = {v: {} for v in versions}
    for members, canon in zip(clusters, canon_keys):
        for ver, e, _ in members:
            old = e.original_key
            if not old:
                warnings.append(
                    f"{tag} stage=bib_unify v{ver}: entry without citation key; "
                    f"canonical key '{canon}' generated (not citable)")
                continue
            prev = mapping[ver].get(old)
            if prev is None:
                mapping[ver][old] = canon
            elif prev != canon:
                warnings.append(
                    f"{tag} stage=bib_unify v{ver}: key '{old}' defined twice with "
                    f"different content; mapping keeps '{prev}'")

    entries = [
        BibEntry(original_key=key, entry_type=etype, fields=fields)
        for key, (etype, fields) in zip(canon_keys, merged)
    ]
    warnings.extend(_possible_duplicates(tag, entries))
    return BibliographyResult(
        arxiv_id=arxiv_id, entries=entries, key_mapping=mapping, warnings=warnings,
    )
