"""Bounded arXiv compilation hints; no TeX execution or external file paths."""
import hashlib
import json

from .schema import relative_path, require_arxiv_id, require_version

MAX_COMPILATION_BYTES = 256 * 1024


def parse_compilation_readme(raw):
    if len(raw) > MAX_COMPILATION_BYTES:
        raise ValueError('compilation README exceeds size limit')
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict) or not isinstance(data.get('sources'), list):
        raise ValueError('compilation README needs sources list')
    if len(data['sources']) > 10000:
        raise ValueError('too many compilation source hints')
    result = {'toplevels': [], 'ignored': []}
    seen = set()
    for row in data['sources']:
        if not isinstance(row, dict) or not isinstance(row.get('filename'), str):
            raise ValueError('invalid compilation source hint')
        name = relative_path(row['filename'])
        if name in seen:
            raise ValueError('duplicate compilation source hint')
        seen.add(name)
        key = {'toplevel': 'toplevels', 'ignore': 'ignored'}.get(row.get('usage'))
        if key:
            result[key].append(name)
    return result


def compilation_record(raw, arxiv_id, version):
    return dict(schema_version=1, arxiv_id=require_arxiv_id(arxiv_id),
                version=require_version(version), readme_file='00README.json',
                readme_sha256=hashlib.sha256(raw).hexdigest(),
                **parse_compilation_readme(raw))
