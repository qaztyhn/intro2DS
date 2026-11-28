"""Offline M3 feature engineering and per-publication candidate pairing."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import re
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
from rapidfuzz import fuzz

from .contracts import (MaskConfig, SCHEMAS, make_table, normalize_arxiv_id,
                        validate_pairs, validate_table)

LOG = logging.getLogger(__name__)
EXTRA_FEATURES = [
    pa.field('title_exact_match', pa.bool_()),
    pa.field('title_token_jaccard', pa.float64()),
    pa.field('title_char_ngram_similarity', pa.float64()),
    pa.field('author_overlap', pa.float64()),
    pa.field('last_author_match', pa.bool_()),
    pa.field('author_count_diff', pa.int32()),
    pa.field('title_length_ratio', pa.float64()),
    pa.field('title_missing', pa.bool_(), nullable=False),
    pa.field('author_missing', pa.bool_(), nullable=False),
    pa.field('year_missing', pa.bool_(), nullable=False),
]
PAIR_SCHEMA = pa.schema(list(SCHEMAS['pairs']) + EXTRA_FEATURES)
FEATURE_NAMES = PAIR_SCHEMA.names[3:]


@dataclass(frozen=True)
class PairBatch:
    queries: pa.Table
    pairs: pa.Table


# Handle common TeX accents before removing grouping and formatting syntax.
_ACCENTS = {'\'': '\u0301', '`': '\u0300', '"': '\u0308', '^': '\u0302',
            '~': '\u0303', '=': '\u0304', '.': '\u0307', 'c': '\u0327',
            'v': '\u030c', 'u': '\u0306', 'H': '\u030b', 'r': '\u030a',
            'k': '\u0328', 'b': '\u0331', 'd': '\u0323'}
_ACCENT_RE = re.compile(r"\\(['`\"^~=.]|[cvuHrkbd](?![A-Za-z]))\s*\{?([A-Za-z])\}?")
_LATEX_LETTERS = {'ae': 'ae', 'AE': 'AE', 'oe': 'oe', 'OE': 'OE', 'ss': 'ss',
                  'o': 'o', 'O': 'O', 'l': 'l', 'L': 'L', 'i': 'i', 'j': 'j'}
_FORMAT_RE = re.compile(r'\\(?:textbf|textit|texttt|textsc|emph|mathrm|mathbf|mathit|operatorname|protect|bfseries|itshape)\b')


def _plain(value: str | None) -> str:
    if not value:
        return ''
    s = _ACCENT_RE.sub(lambda m: m[2] + _ACCENTS[m[1]], value)
    s = re.sub(r'\\([A-Za-z]+)\b', lambda m: _LATEX_LETTERS.get(m[1], m[0]), s)
    s = _FORMAT_RE.sub('', s)
    s = re.sub(r'\\([&%_#$])', r'\1', s)
    s = s.replace('~', ' ')
    s = unicodedata.normalize('NFKD', s.casefold())
    return ''.join(c for c in s if not unicodedata.combining(c))


def normalize_title(title: str | None) -> str:
    s = _plain(title)
    # Braces group text; removing them preserves a word split by TeX grouping.
    s = s.replace('{', '').replace('}', '')
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in s).split())


def normalize_author(name: str | None) -> str:
    return normalize_title(name)


def _surname(name: str | None) -> str | None:
    if not name:
        return None
    # BibTeX "von Last, First" / "von Last, Jr, First" vs natural order.
    if ',' in name:
        tokens = normalize_author(name.split(',', 1)[0]).split()
    else:
        tokens = normalize_author(name).split()
        if len(tokens) > 1 and tokens[-1] in {'jr', 'sr', 'ii', 'iii', 'iv'}:
            tokens.pop()
    if not tokens or tokens[-1] in {'others', 'etal', 'al'}:
        return None
    # Last lexical token is a conservative heuristic, not a full name parser.
    return tokens[-1]


def get_first_author_surname(authors: list[str] | None) -> str | None:
    return _surname(authors[0]) if authors else None


def extract_year(date: str | None) -> int | None:
    if not date:
        return None
    m = re.fullmatch(r'\s*(\d{4})(?:-\d{2}-\d{2}(?:[T ].*)?)?\s*', date)
    return _valid_year(int(m[1])) if m else None


def _valid_year(year):
    return year if isinstance(year, int) and 1000 <= year <= 9999 else None


# Only explicit arXiv contexts are accepted; no guessing from arbitrary numbers.
_ID = r'(?:\d{4}\.\d{4,5}|[a-z]+(?:-[a-z]+)*(?:\.[A-Za-z]{2})?/\d{7})(?:v\d+)?'
_ID_RE = re.compile(r'(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)(' + _ID + r')(?![\w/])', re.I)


def extract_entry_arxiv_ids(entry: dict, *, masked_fields=()) -> set[str]:
    """Extract cited IDs, NEVER entry['arxiv_id'] (the citing-paper key)."""
    blocked = set(masked_fields)
    if 'arxiv_id' in blocked:
        return set()
    result = set()
    for field in ('url', 'doi', 'journal'):
        if field in blocked:
            continue
        text = entry.get(field) or ''
        # The URL can also contain a bare identifier in exported metadata.
        direct = normalize_arxiv_id(text) if field == 'url' else None
        if direct:
            result.add(direct)
        for match in _ID_RE.finditer(text):
            candidate = normalize_arxiv_id(match[1])
            if candidate:
                result.add(candidate)
    return result


def _check_mask(mask):
    if mask is None:
        return
    if not isinstance(mask, MaskConfig):
        raise TypeError('mask must be contracts.MaskConfig or None')
    if not math.isfinite(mask.fraction) or not 0 <= mask.fraction <= 1:
        raise ValueError('mask.fraction must be within [0, 1]')
    if set(mask.fields) - {'url', 'doi', 'arxiv_id'}:
        raise ValueError('unsupported mask fields')


def is_masked(arxiv_id: str, citation_key: str, mask: MaskConfig | None) -> bool:
    if mask is None:
        return False
    _check_mask(mask)
    # JSON encoding avoids ambiguous concatenations of key components.
    payload = json.dumps([arxiv_id, citation_key, mask.seed], ensure_ascii=False,
                         separators=(',', ':')).encode('utf-8')
    value = int.from_bytes(hashlib.sha256(payload).digest()[:8], 'big')
    return value < int(mask.fraction * (1 << 64))


def _trigrams(text):
    return frozenset(text[i:i+3] for i in range(len(text)-2)) if len(text) >= 3 else frozenset([text]) if text else frozenset()


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else None


def _same(a, b):
    return a == b if a is not None and b is not None else None


def _prepared(title, authors, year):
    t = normalize_title(title)
    names = [_surname(a) for a in (authors or [])]
    return {'title': t, 'tokens': frozenset(t.split()), 'grams': _trigrams(t),
            'first': names[0] if names else None, 'last': names[-1] if names else None,
            'authors': frozenset(n for n in names if n),
            'count': len(names) if names and all(names) else None, 'year': year}


def _features(e, r, ids, ref_id):
    a, b = e['title'], r['title']
    ya, yb = e['year'], r['year']
    ca, cb = e['count'], r['count']
    return {
        'title_similarity': fuzz.ratio(a, b) / 100 if a and b else None,
        'first_author_match': _same(e['first'], r['first']),
        'year_diff': abs(ya-yb) if ya is not None and yb is not None else None,
        'arxiv_id_match': ref_id in ids if ids else None,
        'title_exact_match': a == b if a and b else None,
        'title_token_jaccard': _jaccard(e['tokens'], r['tokens']),
        'title_char_ngram_similarity': _jaccard(e['grams'], r['grams']),
        'author_overlap': _jaccard(e['authors'], r['authors']),
        'last_author_match': _same(e['last'], r['last']),
        'author_count_diff': abs(ca-cb) if ca is not None and cb is not None else None,
        'title_length_ratio': min(len(a), len(b))/max(len(a), len(b)) if a and b else None,
        'title_missing': not (a and b),
        'author_missing': not (e['authors'] and r['authors']),
        'year_missing': ya is None or yb is None,
    }


def _id(raw, where):
    result = normalize_arxiv_id(raw) if isinstance(raw, str) else None
    if result is None:
        raise ValueError(f'{where}: invalid arXiv ID {raw!r}')
    return result


def _check_schema(table, name):
    if not isinstance(table, pa.Table):
        raise TypeError(f'[{name}] expected pyarrow.Table')
    # Ignore metadata, but require the exact columns and Arrow types.
    expected = SCHEMAS[name]
    if table.schema.names != expected.names or any(table.schema.field(f.name).type != f.type for f in expected):
        raise ValueError(f'[{name}] incompatible schema: {table.schema}')


def _canonical_rows(table, name, paper):
    _check_schema(table, name)
    result = []
    for row in table.to_pylist():
        row['arxiv_id'] = _id(row['arxiv_id'], f'[{name}]')
        if row['arxiv_id'] != paper:
            raise ValueError(f'[{name}] expected {paper}, got {row["arxiv_id"]}')
        if name == 'references':
            row['ref_arxiv_id'] = _id(row['ref_arxiv_id'], '[references] candidate')
        result.append(row)
    return result


def _dedup_refs(rows):
    def present(v):
        if isinstance(v, str):
            return bool(v.strip())
        if isinstance(v, list):
            return any(present(x) for x in v)
        return v is not None
    fields = SCHEMAS['references'].names[2:]
    kept = {}
    for row in rows:
        key = row['ref_arxiv_id']
        old = kept.get(key)
        if old is not None:
            if any(present(old[f]) and present(row[f]) and old[f] != row[f] for f in fields):
                LOG.warning('[references] conflicting metadata: %s/%s', row['arxiv_id'], key)
            if sum(present(row[f]) for f in fields) <= sum(present(old[f]) for f in fields):
                continue
        kept[key] = row
    return [kept[k] for k in sorted(kept)]


def build_pair_batch(*, arxiv_id, bibtex, references, mask: MaskConfig | None = None) -> PairBatch:
    """For one publication: all m*n candidates, no labels, no mutation."""
    paper = _id(arxiv_id, '[arxiv_id]')
    _check_mask(mask)
    entries = _canonical_rows(bibtex, 'bibtex', paper)
    refs = _dedup_refs(_canonical_rows(references, 'references', paper))
    entries_table = make_table('bibtex', entries)
    validate_table('bibtex', entries_table)
    refs_table = make_table('references', refs)
    validate_table('references', refs_table)
    entries.sort(key=lambda e: e['citation_key'])
    queries = make_table('queries', [{'arxiv_id': paper, 'citation_key': e['citation_key']} for e in entries])
    prepared_refs = [(r['ref_arxiv_id'], _prepared(r['ref_title'], r['ref_authors'], extract_year(r['ref_submission_date']))) for r in refs]
    rows = []
    for e in entries:
        blocked = mask.fields if is_masked(paper, e['citation_key'], mask) else ()
        ids = extract_entry_arxiv_ids(e, masked_fields=blocked)
        prepared = _prepared(e['title'], e['authors'], _valid_year(e['year']))
        for ref_id, r in prepared_refs:
            rows.append({'arxiv_id': paper, 'citation_key': e['citation_key'], 'ref_arxiv_id': ref_id,
                         **_features(prepared, r, ids, ref_id)})
    pairs = pa.Table.from_pylist(rows, schema=PAIR_SCHEMA) if rows else PAIR_SCHEMA.empty_table()
    validate_pairs(queries, refs_table, pairs)
    return PairBatch(queries, pairs)


def _read_bibtex(path, wanted):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_file():
        parquet = pq.ParquetFile(path)
        schema = parquet.schema_arrow
        batches = parquet.iter_batches(batch_size=32768)
    else:
        files = sorted(path.rglob('*.parquet'))
        if not files:
            raise FileNotFoundError(f'No Parquet under {path}')
        dataset = ds.dataset([str(p) for p in files], format='parquet')
        for fragment in dataset.get_fragments():
            if not fragment.physical_schema.equals(SCHEMAS['bibtex'], check_metadata=False):
                raise ValueError(f'[bibtex] incompatible schema: {fragment.path}')
        schema = dataset.schema
        batches = dataset.to_batches(batch_size=32768)
    _check_schema(schema.empty_table(), 'bibtex')
    grouped = defaultdict(list)
    for batch in batches:
        for row in batch.to_pylist():
            a = _id(row['arxiv_id'], '[bibtex]')
            if a in wanted:
                row['arxiv_id'] = a
                grouped[a].append(row)
    return grouped


def _group(table, wanted, name):
    grouped = defaultdict(list)
    # Normalize the citing ID before selecting so versioned IDs cannot be lost.
    for batch in table.to_batches(max_chunksize=32768):
        for row in batch.to_pylist():
            a = _id(row['arxiv_id'], f'[{name}]')
            if a != row['arxiv_id']:
                raise ValueError(f'[{name}] noncanonical catalog ID: {row["arxiv_id"]}')
            if a in wanted:
                grouped[a].append(row)
    return grouped


def prepare_pairs(*, catalog_dir, bibtex_path, publication_ids, output_dir, mask=None) -> Path:
    """Write pairs.parquet + pairing_report.csv to a new directory.

    Per-paper validation enforces the complete m*n product. Failed/empty papers
    remain visible in the report; only eligible rows may be used for labeling.
    """
    _check_mask(mask)
    if isinstance(publication_ids, (str, bytes, Path)):
        raise TypeError('publication_ids must be an iterable of IDs, not a filename')
    ids = [_id(a, '[publication_ids]') for a in publication_ids]
    if len(ids) != len(set(ids)):
        raise ValueError('[publication_ids] duplicate normalized IDs')
    ids.sort()
    wanted = set(ids)
    catalog = Path(catalog_dir).resolve()
    out = Path(output_dir).resolve()
    for source in (catalog, Path(bibtex_path).resolve()):
        if out == source or source in out.parents or out in source.parents:
            raise ValueError(f'output_dir overlaps input: {source}')
    if out.exists():
        raise FileExistsError(out)
    grouped = {}
    for name in ('papers', 'references', 'queries', 'crawl_status'):
        table = pq.read_table(catalog / f'{name}.parquet')
        validate_table(name, table)
        grouped[name] = _group(table, wanted, name)
    for name in ('papers', 'crawl_status'):
        missing = wanted - grouped[name].keys()
        if missing:
            raise ValueError(f'[{name}] selected IDs absent: {sorted(missing)[:20]}')
    bib = _read_bibtex(bibtex_path, wanted)
    report = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.pairs-', dir=out.parent) as tmp:
        stage = Path(tmp) / 'output'
        stage.mkdir()
        with pq.ParquetWriter(stage / 'pairs.parquet', PAIR_SCHEMA, compression='zstd') as writer:
            for a in ids:
                entries = make_table('bibtex', bib[a])
                refs = make_table('references', grouped['references'][a])
                status = grouped['crawl_status'][a][0]
                validate_table('references', refs)
                if any(_id(r['ref_arxiv_id'], '[references]') != r['ref_arxiv_id'] for r in grouped['references'][a]):
                    raise ValueError(f'[references] {a}: noncanonical candidate ID in catalog')
                if status['candidate_count'] != refs.num_rows:
                    raise ValueError(f'[crawl_status] {a}: candidate_count differs from references')
                batch = build_pair_batch(arxiv_id=a, bibtex=entries, references=refs, mask=mask)
                keys = {r['citation_key'] for r in grouped['queries'][a]}
                actual = set(batch.queries['citation_key'].to_pylist())
                if keys != actual:
                    raise ValueError(f'[queries] {a}: catalog/BibTeX mismatch; check M2 snapshot')
                if batch.pairs.num_rows != entries.num_rows * refs.num_rows:
                    raise ValueError(f'[pairs] {a}: incorrect m*n')
                if batch.pairs.num_rows:
                    writer.write_table(batch.pairs)
                reasons = []
                if status['status'] not in {'ok', 'empty'}:
                    reasons.append('unusable_crawl')
                if not entries.num_rows:
                    reasons.append('no_bibtex')
                if not refs.num_rows:
                    reasons.append('no_candidates')
                report.append({'arxiv_id': a, 'crawl_status': status['status'],
                               'bibtex_count': entries.num_rows, 'candidate_count': refs.num_rows,
                               'pair_count': batch.pairs.num_rows, 'eligible': not reasons,
                               'reason': ';'.join(reasons)})
        columns = ['arxiv_id', 'crawl_status', 'bibtex_count', 'candidate_count', 'pair_count', 'eligible', 'reason']
        with (stage / 'pairing_report.csv').open('w', encoding='utf-8', newline='') as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            w.writerows(report)
        if out.exists():
            raise FileExistsError(out)
        stage.rename(out)
    return out
