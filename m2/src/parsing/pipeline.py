"""Publication-scoped orchestration. M1 artifacts and S2 data are never changed."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from m1.src.schema import relative_path
from .bibliography import build_key_resolver, cited_keys, extract_bibliography_detailed, unify_bibliography
from .contracts import GatheredDocument, ParsedDocument
from .diagnostics import diagnostic_codes
from .deduplication import deduplicate_with_stats
from .export import bibtex_rows
from .gathering import gather_source_for_review, gather_source_with_root
from .hierarchy import parse_hierarchy
from .m1_adapter import load_recovery_warnings, load_versions
from .qa_report import status_row

LOGGER = logging.getLogger(__name__)


@dataclass
class PublicationResult:
    elements: list[dict] = field(default_factory=list)
    hierarchy: list[dict] = field(default_factory=list)
    bibtex: list[dict] = field(default_factory=list)
    version_status: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bib_entries_before: int = 0
    stage_seconds: dict[str, float] = field(default_factory=dict)
    root_reviews: list[dict] = field(default_factory=list, init=False)
    bibliography_reconciliation: list[dict] = field(default_factory=list)


def validate_root_choice(identity, version, choice):
    context = f'{identity}v{version} stage=root_selection INVALID_ROOT_SELECTION'
    if not isinstance(choice, dict) or set(choice) != {'root_file', 'reason'}:
        raise ValueError(f'{context}: each root choice requires root_file and reason')
    if not isinstance(choice['reason'], str) or not choice['reason'].strip():
        raise ValueError(f'{context}: root choice reason must be nonempty')
    try:
        name = relative_path(choice['root_file'])
    except ValueError as exc:
        raise ValueError(f'{context}: {exc}') from exc
    return dict(root_file=name, reason=choice['reason'])


def process_publication_detailed(run_dir: Path, arxiv_id: str, *, root_choices=None) -> PublicationResult:
    """Keep recoverable partial versions, and report failed/no-TeX versions in QA.

    Adapter/publication-level errors propagate to the batch runner. Exceptions at
    a version stage are logged and recorded without dropping its sibling versions.
    Bibliography mapping is completed before hierarchy normalization.
    """
    result = PublicationResult()

    def measured(stage, function, *args):
        start = perf_counter()
        try:
            return function(*args)
        finally:
            result.stage_seconds[stage] = result.stage_seconds.get(stage, 0) + perf_counter() - start

    versions = measured('adapter', load_versions, Path(run_dir), arxiv_id)
    if not versions:
        raise ValueError(f'{arxiv_id} version=unknown stage=adapter NO_RECORDED_VERSIONS')
    choices = {v: validate_root_choice(arxiv_id, v, c) for v, c in (root_choices or {}).items()}
    if set(choices) - {source.version for source in versions}:
        raise ValueError(f'{arxiv_id} stage=root_selection choice refers to an unknown version')
    gathered = []
    recovery_diagnostics = {}
    for source in versions:
        try:
            if source.version in choices:
                choice = choices[source.version]
                doc = measured('gather', gather_source_with_root, source, choice['root_file'])
                doc.warnings.append(f'{arxiv_id}v{source.version} stage=root_selection reason={choice["reason"]}')
            else:
                doc = measured('gather', gather_source_for_review, source, result.root_reviews)
            if doc.status in {'ok', 'partial'}:
                # Gathering labels every warning partial. Defer missing-resource impact
                # until citation resolution, without masking independent hierarchy warnings.
                if doc.status == 'partial' and doc.warnings and not diagnostic_codes(doc.warnings)[0]:
                    doc.status = 'ok'
                issues = load_recovery_warnings(Path(run_dir), arxiv_id, source.version)
                if issues:
                    messages = [f'{arxiv_id}v{source.version} stage=bibliography '
                                f'RECOVERY_CONVERSION_WARNING: {issue}' for issue in issues]
                    recovery_diagnostics[source.version] = messages
                    doc.warnings.extend(messages)
        except Exception as error:
            LOGGER.exception('%s v%s stage=gather', arxiv_id, source.version)
            doc = GatheredDocument(arxiv_id, source.version, None, '', [], {}, 'failed',
                                   [f'{arxiv_id}v{source.version} stage=gather EXCEPTION: {error}'])
        gathered.append(doc)
    entries, errors, bib_diagnostics = {}, {}, {}
    for doc in gathered:
        if doc.status in {'ok', 'partial'}:
            try:
                def extract_with_provenance():
                    return extract_bibliography_detailed(doc,
                        conflict_sink=result.bibliography_reconciliation)
                entries[doc.version], bib_diagnostics[doc.version] = measured(
                    'bibliography_extract', extract_with_provenance)
                bib_diagnostics[doc.version].extend(recovery_diagnostics.get(doc.version, []))
            except Exception as error:  # isolation boundary, never silent
                errors[doc.version] = ('bibliography_extract', str(error))
                LOGGER.exception('%s v%s stage=bibliography_extract', arxiv_id, doc.version)
    result.bib_entries_before = sum(map(len, entries.values()))
    bibliography = measured('bibliography_merge', unify_bibliography, arxiv_id, entries)
    result.warnings.extend(bibliography.warnings)
    known_keys = {entry.original_key for entry in bibliography.entries}
    parsed = []
    for source, doc in zip(versions, gathered):
        error = errors.get(doc.version)
        tree = None
        if error is None:
            try:
                tree = measured('hierarchy', parse_hierarchy, doc,
                                bibliography.key_mapping.get(doc.version, {}))
            except Exception as exc:
                error = ('hierarchy', str(exc))
                LOGGER.exception('%s v%s stage=hierarchy', arxiv_id, doc.version)
        if tree is None:
            tree = ParsedDocument(arxiv_id, doc.version, None, 'failed',
                                  list(doc.warnings) + [f'{arxiv_id}v{doc.version} stage={error[0]} EXCEPTION: {error[1]}'])
        missing = set()
        diagnostics = bib_diagnostics.get(doc.version, recovery_diagnostics.get(doc.version, []))
        tree.warnings.extend(w for w in diagnostics if w not in tree.warnings)
        if tree.root is not None:
            resolve_key = build_key_resolver({**{key: key for key in known_keys},
                                             **bibliography.key_mapping.get(doc.version, {})})
            pending = [tree.root]
            while pending:
                node = pending.pop()
                missing.update(key for key in cited_keys(node.content)
                               if resolve_key(key) not in known_keys)
                pending.extend(node.children)
            if missing:
                tree.warnings.append(f'{arxiv_id}v{doc.version} stage=bibliography '
                                     'MISSING_CITATION_KEYS: ' + ', '.join(sorted(missing)))
        reasons, info = diagnostic_codes(tree.warnings, missing_keys=bool(missing))
        if reasons and tree.status == 'ok':
            tree.status = 'partial'
        parsed.append(tree)
        row = status_row(
            doc, tree, has_tex=any(name.lower().endswith('.tex') for name in source.source_files),
            error=error)
        row.update(partial_reasons=reasons, info_diagnostics=info,
                   bibliography_parse_warnings=len(bib_diagnostics.get(doc.version, [])),
                   missing_citation_keys=sorted(missing), root_selection=choices.get(doc.version),
                   missing_bibliography_resources=[w for w in doc.warnings if ' MISSING_BIB_RESOURCE:' in w])
        if row['status'] == 'partial' and (diagnostics or missing):
            row.update(stage='bibliography', reason='MISSING_CITATION_KEYS' if missing else 'BIBLIOGRAPHY_WARNINGS')
        result.version_status.append(row)
        result.warnings.extend(tree.warnings)
        LOGGER.info('%s v%s stage=pipeline status=%s warnings=%s',
                    arxiv_id, doc.version, tree.status, tree.warnings)
    result.elements, result.hierarchy, stats = measured('deduplicate', deduplicate_with_stats, parsed)
    result.warnings.extend(stats['warnings'])
    result.bibtex = bibtex_rows(arxiv_id, bibliography.entries)
    return result


def process_publication(run_dir: Path, arxiv_id: str):
    """Contract interface, directly consumable by the benchmark harness."""
    result = process_publication_detailed(run_dir, arxiv_id)
    return result.elements, result.hierarchy, result.bibtex
