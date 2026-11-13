"""Read every recorded M1 version without touching S2 data or mutating M1."""
import logging
import re
from pathlib import Path

from ..schema import no_symlinks, paper_directory, require_arxiv_id, safe_path
from ..stages.storage import load_metadata, load_manifest
from ..stages.validation import validate_manifest
from .contracts import SourceVersion

LOGGER = logging.getLogger(__name__)


class M1AdapterError(ValueError):
    """Publication-level input error, to be caught by future orchestration."""
    def __init__(self, arxiv_id, code, detail):
        self.arxiv_id, self.stage, self.code = arxiv_id, 'm1_adapter', code
        super().__init__(f'{arxiv_id} stage=m1_adapter {code}: {detail}')


def load_versions(run_dir: Path, arxiv_id: str) -> list[SourceVersion]:
    """Return metadata/observed version union; missing manifests become failures.

    References are deliberately not read. M1 load_paper(cfg, id) eagerly reads
    references.json, so its narrower metadata/manifest helpers are reused here.
    """
    try:
        require_arxiv_id(arxiv_id)
        run = no_symlinks(Path(run_dir).absolute())
        if not run.is_dir(): raise ValueError('run directory does not exist')
        cfg = {'output_root': run.parent, 'run_id': run.name}
        paper = paper_directory(cfg, arxiv_id)
        metadata = load_metadata(cfg, arxiv_id)
    except (OSError, ValueError) as exc:
        raise M1AdapterError(arxiv_id, 'INVALID_INPUT', str(exc)) from exc
    numbers = set()
    if metadata is not None:
        if metadata.get('arxiv_id') != arxiv_id:
            raise M1AdapterError(arxiv_id, 'IDENTITY_MISMATCH', 'metadata ID differs from requested ID')
        expected = metadata.get('versions')
        if expected is not None:
            if (not isinstance(expected, list) or any(type(v) is not int or v < 1 for v in expected)
                    or len(expected) != len(set(expected))):
                raise M1AdapterError(arxiv_id, 'INVALID_VERSIONS', 'metadata versions must be distinct positive integers')
            numbers.update(expected)
    else:
        LOGGER.warning('%s stage=m1_adapter MISSING_METADATA: use observed manifests only', arxiv_id)
    try:
        directory = safe_path(paper, 'versions')
        if directory.exists():
            for entry in sorted(directory.iterdir()):
                no_symlinks(entry)
                if not entry.is_dir(): continue
                if not re.fullmatch(r'v[1-9]\d*', entry.name):
                    raise ValueError('noncanonical version directory: ' + entry.name)
                number = int(entry.name[1:])
                if metadata is not None and number not in numbers:
                    LOGGER.warning('%sv%d stage=m1_adapter VERSION_NOT_IN_METADATA', arxiv_id, number)
                numbers.add(number)
    except (OSError, ValueError) as exc:
        raise M1AdapterError(arxiv_id, 'INVALID_VERSION_DIRECTORY', str(exc)) from exc
    results = []
    for number in sorted(numbers):
        source = paper / 'versions' / f'v{number}' / 'source'
        code = None
        try:
            safe_path(paper, f'versions/v{number}/source')
            manifest = load_manifest(cfg, arxiv_id, number)
            if manifest is None:
                code = 'MISSING_MANIFEST'
            elif any(i['severity'] == 'error' for i in validate_manifest(manifest, arxiv_id=arxiv_id, version=number)):
                code = 'INVALID_MANIFEST'
        except (OSError, ValueError) as exc:
            code = 'UNREADABLE_OR_UNSAFE_MANIFEST'
            LOGGER.warning('%sv%d stage=m1_adapter %s: %s', arxiv_id, number, code, exc)
        if code:
            LOGGER.warning('%sv%d stage=m1_adapter %s', arxiv_id, number, code)
            results.append(SourceVersion(arxiv_id, number, source, (), 'failed', 'failed', code))
        else:
            results.append(SourceVersion(arxiv_id, number, source, tuple(manifest['source_files']),
                manifest['download_status'], manifest['extract_status'], manifest.get('error_code')))
    if not results:
        LOGGER.warning('%s stage=m1_adapter NO_RECORDED_VERSIONS', arxiv_id)
    return results
