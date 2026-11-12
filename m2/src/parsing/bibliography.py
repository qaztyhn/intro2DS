"""Bibliography extraction (TV3).

Stage 2: raw .bib parsing. Field normalization, \\bibitem parsing and
cross-version unification are added in later stages.
"""
from __future__ import annotations

import logging
import re
import unicodedata

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


# --------------------------------------------------------------------------
# \bibitem / thebibliography / .bbl
# --------------------------------------------------------------------------
_COMMENT = re.compile(r"(?<!\\)%[^\n]*")
_BIBITEM = re.compile(
    r"\\bibitem\s*(?:\[((?:[^\[\]{}]|\{[^{}]*\})*)\])?\s*\{([^{}]*)\}"
)
_END_BIB = re.compile(r"\\end\s*\{thebibliography\}")
_NEWBLOCK = re.compile(r"\\newblock\b")
_DOI = re.compile(r"10\.\d{4,9}/[^\s,;{}\]\"<>]+")
_URL = re.compile(r"\\(?:url|href)\s*\{([^{}]*)\}|(https?://[^\s{}\\]+)")
_ARXIV = re.compile(
    r"arXiv\s*:\s*(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})", re.I
)
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _bibitem_fields(body: str, label: str | None) -> dict[str, str]:
    fields: dict[str, str] = {"raw": _collapse(body)}
    if label is not None:
        fields["label"] = _collapse(label)

    m = _DOI.search(body)
    if m:
        fields["doi"] = m.group(0).rstrip(".)")
    m = _URL.search(body)
    if m:
        fields["url"] = (m.group(1) or m.group(2)).rstrip(".,;)")
    m = _ARXIV.search(body)
    if m:
        fields["eprint"] = m.group(1)

    # Year: last 4-digit year outside URLs / DOIs / arXiv ids.
    cleaned = _ARXIV.sub(" ", _DOI.sub(" ", _URL.sub(" ", body)))
    years = _YEAR.findall(cleaned)
    if years:
        fields["year"] = years[-1]

    blocks = [_collapse(b) for b in _NEWBLOCK.split(body)]
    blocks = [b for b in blocks if b]
    if len(blocks) >= 2:
        fields["author"] = blocks[0]
        fields["title"] = blocks[1]
        if len(blocks) >= 3:
            fields["journal"] = blocks[2]
    return fields


def parse_bibitems(text: str) -> tuple[list[BibEntry], list[str]]:
    """Parse \\bibitem entries from thebibliography / .bbl text."""
    text = _COMMENT.sub("", text)
    matches = list(_BIBITEM.finditer(text))
    entries: list[BibEntry] = []
    warnings: list[str] = []
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[m.end():end]
        stop = _END_BIB.search(body)
        if stop:
            body = body[:stop.start()]
        key = m.group(2).strip()
        if not key:
            warnings.append(f"\\bibitem #{idx + 1} has an empty key; skipped")
            continue
        entries.append(BibEntry(
            original_key=key,
            entry_type="misc",
            fields=_bibitem_fields(body, m.group(1)),
        ))
    return entries, warnings


def extract_bibliography(document: GatheredDocument) -> list[BibEntry]:
    """Extract raw entries of one version: .bib, then .bbl, then inline \\bibitem."""
    tag = f"[{document.arxiv_id} v{document.version}]"
    sources: list[tuple[str, list[BibEntry], list[str]]] = []
    paths = sorted(document.bib_files)
    for path in [p for p in paths if not p.lower().endswith(".bbl")]:
        sources.append((path, *parse_bib(document.bib_files[path])))
    for path in [p for p in paths if p.lower().endswith(".bbl")]:
        sources.append((path, *parse_bibitems(document.bib_files[path])))
    sources.append(("<latex>", *parse_bibitems(document.latex)))

    entries: list[BibEntry] = []
    seen: set[str] = set()
    for path, parsed, warns in sources:
        for w in warns:
            log.warning("%s stage=bib_parse file=%s: %s", tag, path, w)
        for e in parsed:
            if e.original_key in seen:
                log.warning("%s stage=bib_parse file=%s: duplicate key '%s' "
                            "skipped (first kept)", tag, path, e.original_key)
                continue
            seen.add(e.original_key)
            entries.append(e)
    return [normalize_entry(e) for e in entries]

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
    for idx, part in enumerate(_MATH_SPLIT.split(str(s))):
        if idx % 2 == 1:
            out.append(part)
            continue
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
    if name.strip().lower() == "others":
        return None
    pieces = _split_depth0(name, _COMMA)
    if len(pieces) == 2:
        text = f"{pieces[1]} {pieces[0]}"
    elif len(pieces) == 3:
        text = f"{pieces[2]} {pieces[0]} {pieces[1]}"
    else:
        text = name
    return clean_text(text) or None


def _authors_from_bibtex(s: str) -> list[str]:
    names = (_format_name(p) for p in _split_depth0(s, _AND))
    return [n for n in names if n]


def _authors_from_bibitem(s: str) -> list[str]:
    s = re.sub(r"(?<![A-Z])\.\s*$", "", s.strip())
    s = re.sub(r",\s+and\s+", " and ", s)
    result: list[str] = []
    for chunk in _split_depth0(s, _AND):
        merged: list[str] = []
        for tok in _split_depth0(chunk, _COMMA):
            plain = clean_text(tok)
            if merged and _INITIALS.match(plain):
                merged[-1] = f"{plain} {merged[-1]}"
            else:
                merged.append(plain)
        result.extend(n for n in merged if n and n.lower() != "others")
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
    return m2.group(0).rstrip(".,;)") if m2 else None


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

    jr = f.get("journal") or f.get("journaltitle") or ""
    if from_bibitem:
        m = _EMPH.search(jr)
        jr = m.group(1) if m else jr
    journal = clean_text(jr) or None

    f.update(
        title=title or None,
        authors=authors,
        year=_year(f),
        doi=_doi(f),
        journal=journal,
        url=_url(f),
    )
    etype = entry.entry_type.strip().lower()
    return BibEntry(
        original_key=entry.original_key,
        entry_type=_TYPE_ALIAS.get(etype, etype),
        fields=f,
    )