"""TV1: bounded literal LaTeX gathering. No compilation, writes or network.

This is not a TeX macro interpreter. Unknown conditions/dynamic dependencies
remain visible with partial status, instead of pretending to follow both paths.
"""
from __future__ import annotations

import posixpath
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..schema import no_symlinks, relative_path, require_arxiv_id, require_version
from .contracts import GatheredDocument, SourceVersion

MAX_FILE_BYTES = 64 * 2**20
MAX_TOTAL_BYTES = 512 * 2**20
MAX_OUTPUT_CHARS = 128 * 2**20
MAX_DEPTH = 128
LITERALS = {'verbatim', 'verbatim*', 'Verbatim', 'lstlisting', 'minted', 'comment'}
DEFINITIONS = {'newcommand', 'renewcommand', 'providecommand', 'DeclareRobustCommand',
               'DeclareMathOperator', 'def', 'gdef', 'edef', 'xdef', 'newenvironment', 'renewenvironment'}
DIRECTIVES = re.compile(r'\\(?:input|include|import|subimport|bibliography|addbibresource|documentclass)(?![A-Za-z@])')
COMMAND = re.compile(r'\\([A-Za-z@]+|[^\n])')
SPECIAL = re.compile(r'[\\%]')
CONDITIONAL_MACROS = {'ifthenelse', 'ifdef', 'ifboolexpr', 'ifstrequal', 'IfFileExists'}


def is_conditional(name):
    # \iff is a mathematical relation, not a primitive/boolean conditional.
    return name.startswith('if') and name != 'iff' and name not in CONDITIONAL_MACROS


class GatheringError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


def command(text, start):
    match = COMMAND.match(text, start)
    return (match[1], match.end()) if match else ('', start + 1)


def spaces(text, start):
    while start < len(text) and text[start].isspace(): start += 1
    return start


def group(text, start, opening='{', closing='}'):
    start = spaces(text, start)
    if start >= len(text) or text[start] != opening: return None
    depth, end = 1, start + 1
    while end < len(text):
        if text[end] == '\\':
            _, end = command(text, end)
            continue
        if text[end] == opening: depth += 1
        if text[end] == closing:
            depth -= 1
            if depth == 0: return text[start + 1:end], end + 1
        end += 1
    return None


def literal(text, start):
    name, end = command(text, start)
    if name == 'verb':
        if end < len(text) and text[end] == '*': end += 1
        if end < len(text) and not text[end].isspace():
            closing = text.find(text[end], end + 1)
            newline = text.find('\n', end + 1)
            if closing >= 0 and (newline < 0 or closing < newline): return 'verb', closing + 1, True
            return 'verb', newline if newline >= 0 else len(text), False
    if name == 'begin':
        value = group(text, end)
        if value and value[0] in LITERALS:
            match = re.search(r'\\end\s*\{' + re.escape(value[0]) + r'\}', text[value[1]:])
            return (value[0], value[1] + match.end(), True) if match else (value[0], len(text), False)
    return None


def strip_comments(text):
    """Respect escaped percent and literal bodies; TeX % consumes the newline."""
    output, warnings, index = [], [], 0
    while index < len(text):
        if text[index] == '\\':
            special = literal(text, index)
            if special:
                kind, end, closed = special
                if kind != 'comment': output.append(text[index:end])
                if not closed: warnings.append('UNTERMINATED_LITERAL')
                index = end
                continue
            _, end = command(text, index)
            output.append(text[index:end]); index = end
        elif text[index] == '%':
            end = text.find('\n', index)
            index = len(text) if end < 0 else end + 1
        else:
            marker = SPECIAL.search(text, index)
            end = marker.start() if marker else len(text)
            output.append(text[index:end]); index = end
    return ''.join(output), warnings


def definition(text, name, end):
    start = spaces(text, end)
    if start < len(text) and text[start] == '*': start = spaces(text, start + 1)
    target = group(text, start)
    if target: start = target[1]
    elif start < len(text) and text[start] == '\\': _, start = command(text, start)
    else: return None
    if name in ('def', 'gdef', 'edef', 'xdef'):
        start = text.find('{', start)
        if start < 0: return None
    else:
        for _ in range(2):
            optional = group(text, start, '[', ']')
            if optional: start = optional[1]
    body = group(text, start)
    if not body: return None
    bodies = body[0]
    if name in ('newenvironment', 'renewenvironment'):
        second = group(text, body[1])
        if not second: return None
        bodies += second[0]
        body = second
    return bodies, body[1]


def conditional(text, end):
    depth, index, otherwise = 1, end, None
    while index < len(text):
        if text[index] != '\\': index += 1; continue
        special = literal(text, index)
        if special: index = special[1]; continue
        name, after = command(text, index)
        if name == 'newif':
            target = spaces(text, after)
            _, index = command(text, target) if target < len(text) and text[target] == '\\' else ('', after)
            continue
        if name in DEFINITIONS:
            body = definition(text, name, after)
            if body: index = body[1]; continue
        if is_conditional(name):
            depth += 1
        elif name == 'fi':
            depth -= 1
            if depth == 0:
                return (text[end:otherwise[0]] if otherwise else text[end:index],
                        text[otherwise[1]:index] if otherwise else '', after)
        elif name == 'else' and depth == 1: otherwise = (index, after)
        index = after
    return None


def events(text):
    """Yield nonoverlapping (start, end, kind, payload) lexical events."""
    index = 0
    while index < len(text):
        if text[index] != '\\': index += 1; continue
        start = index
        special = literal(text, start)
        if special: index = special[1]; continue
        name, end = command(text, start)
        index = end
        if name == 'newif':
            target = spaces(text, end)
            if target < len(text) and text[target] == '\\': _, index = command(text, target)
            continue
        if name in DEFINITIONS:
            body = definition(text, name, end)
            if body:
                index = body[1]
                yield start, index, 'definition', body[0]
            else:
                yield start, end, 'unsupported', 'MALFORMED_MACRO_DEFINITION'
            continue
        if is_conditional(name):
            branches = conditional(text, end)
            if branches:
                yes, no, index = branches
                payload = yes if name == 'iftrue' else no if name == 'iffalse' else None
                yield start, index, 'conditional', payload
            else:
                index = len(text)
                yield start, index, 'unsupported', 'UNTERMINATED_CONDITIONAL'
            continue
        if name == 'unless':
            after = spaces(text, end)
            _, condition_end = command(text, after) if after < len(text) and text[after] == '\\' else ('', after)
            branches = conditional(text, condition_end)
            index = branches[2] if branches else len(text)
            yield start, index, 'conditional', None
            continue
        if name in CONDITIONAL_MACROS:
            for argument in range(3):
                value = group(text, index)
                if value: index = value[1]
                elif argument == 0:
                    token = spaces(text, index)
                    if token < len(text) and text[token] == '\\': _, index = command(text, token)
            yield start, index, 'conditional', None
            continue
        if name == 'documentclass':
            yield start, end, 'root', None
        elif name in ('input', 'include', 'bibliography', 'addbibresource', 'includeonly', 'subfile', 'subfileinclude'):
            after = end
            if name == 'addbibresource':
                optional = group(text, after, '[', ']')
                if optional: after = optional[1]
            value = group(text, after)
            if value:
                index = value[1]
                yield start, index, name, value[0]
            elif name == 'input':
                after = spaces(text, after)
                match = re.match(r'[^\s{}]+', text[after:])
                if match:
                    index = after + len(match[0])
                    yield start, index, name, match[0]
                else: yield start, end, 'unsupported', 'MALFORMED_INPUT'
            else: yield start, end, 'unsupported', 'MALFORMED_' + name.upper()
        elif name in ('import', 'subimport', 'inputfrom', 'includefrom'):
            directory = group(text, end)
            value = group(text, directory[1]) if directory else None
            if directory and value:
                index = value[1]
                yield start, index, 'input', posixpath.join(directory[0], value[0])
            else: yield start, end, 'unsupported', 'MALFORMED_IMPORT'
        elif name == 'endinput':
            line = text.find('\n', end)
            # TeX stops at the end of the current input line.
            index = len(text)
            yield start, index, 'endinput', text[end:line] if line >= 0 else text[end:]


def root_count(text, depth=0):
    if depth > MAX_DEPTH: raise GatheringError('NESTING_LIMIT', 'conditional nesting exceeds limit')
    count = 0
    for _, _, kind, payload in events(text):
        if kind == 'root': count += 1
        elif kind == 'conditional' and payload is not None:
            count += root_count(payload, depth + 1)
    return count


class Context:
    def __init__(self, source):
        self.source = source
        self.base = no_symlinks(Path(source.source_dir).absolute())
        if not self.base.is_dir(): raise GatheringError('MISSING_SOURCE_DIRECTORY', str(self.base))
        self.names = set()
        for name in source.source_files:
            relative = relative_path(name)
            if PurePosixPath(relative).suffix.lower() not in ('.tex', '.bib'):
                raise GatheringError('INVALID_SOURCE_TYPE', relative)
            if relative in self.names: raise GatheringError('DUPLICATE_SOURCE_PATH', relative)
            self.names.add(relative)
        self.cache, self.clean_cache = {}, {}
        self.bytes_read = 0
        self.warnings, self.included, self.bibs = [], [], {}
        self.pending_warnings = {}
        self.root, self.includeonly = None, None

    def warn(self, code, origin='', detail=''):
        if self.root is None and origin in self.names:
            self.pending_warnings.setdefault(origin, []).append((code, detail))
            return
        message = f'{self.source.arxiv_id}v{self.source.version} stage=gather {code}: {origin} {detail}'.rstrip()
        if message not in self.warnings: self.warnings.append(message)

    def read(self, name):
        if name in self.cache: return self.cache[name]
        if name not in self.names: raise GatheringError('UNDECLARED_SOURCE', name)
        target = no_symlinks(self.base / relative_path(name))
        if not target.resolve().is_relative_to(self.base.resolve()):
            raise GatheringError('UNSAFE_SOURCE', name)
        with target.open('rb') as handle: data = handle.read(MAX_FILE_BYTES + 1)
        self.bytes_read += len(data)
        if len(data) > MAX_FILE_BYTES or self.bytes_read > MAX_TOTAL_BYTES:
            raise GatheringError('SOURCE_SIZE_LIMIT', name)
        try: text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            if data.startswith((b'\xff\xfe', b'\xfe\xff')):
                text = data.decode('utf-16')
                self.warn('ENCODING_UTF16', name)
            else:
                text = data.decode('latin-1')
                self.warn('ENCODING_LATIN1_FALLBACK', name, 'byte-preserving fallback; encoding not established')
        if '\x00' in text: raise GatheringError('NUL_BYTES', name)
        self.cache[name] = text
        return text

    def clean(self, name):
        if name not in self.clean_cache:
            text, warnings = strip_comments(self.read(name))
            for code in warnings: self.warn(code, name)
            self.clean_cache[name] = text
        return self.clean_cache[name]

    def resolve(self, origin, raw, suffix):
        raw = raw.strip()
        if not raw or any(c in raw for c in '\\#$\x00{}'):
            self.warn('DYNAMIC_PATH', origin, raw); return None
        if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).drive or ':' in raw:
            self.warn('UNSAFE_PATH', origin, raw); return None
        candidates, safe = set(), False
        bases = (posixpath.dirname(origin), posixpath.dirname(self.root or ''), '')
        for base in bases:
            path = posixpath.normpath(posixpath.join(base, raw))
            if path == '..' or path.startswith('../'): continue
            safe = True
            if not PurePosixPath(path).suffix: path += suffix
            if path in self.names: candidates.add(path)
        if not candidates:
            self.warn('MISSING_DEPENDENCY' if safe else 'UNSAFE_PATH', origin, raw)
            return None
        if len(candidates) > 1:
            self.warn('AMBIGUOUS_PATH', origin, raw + ' -> ' + ', '.join(sorted(candidates)))
            return None
        return next(iter(candidates))

    def expand(self, name, stack=()):
        if name in stack:
            self.warn('INCLUDE_CYCLE', name, ' -> '.join((*stack, name)))
            return None
        if len(stack) >= MAX_DEPTH: raise GatheringError('INCLUDE_DEPTH_LIMIT', name)
        try: text = self.clean(name)
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, GatheringError) and exc.code.endswith('LIMIT'): raise
            self.warn(getattr(exc, 'code', 'SOURCE_READ_FAILED'), name, str(exc))
            return None
        if name not in self.included: self.included.append(name)
        for code, detail in self.pending_warnings.pop(name, []): self.warn(code, name, detail)
        return self.expand_text(text, name, (*stack, name))

    def expand_text(self, text, origin, stack, depth=0):
        if depth > MAX_DEPTH: raise GatheringError('NESTING_LIMIT', origin)
        pieces, position, size = [], 0, 0
        for start, end, kind, payload in events(text):
            replacement = text[start:end]
            if kind == 'definition':
                if DIRECTIVES.search(payload): self.warn('DEFERRED_MACRO_DIRECTIVE', origin)
            elif kind == 'conditional':
                if payload is None: self.warn('UNRESOLVED_CONDITIONAL', origin)
                else: replacement = self.expand_text(payload, origin, stack, depth + 1)
            elif kind == 'endinput':
                replacement = payload
                if payload.strip(): self.warn('ENDINPUT_TRAILING_TOKENS', origin, 'same-line tokens left unexpanded')
            elif kind == 'unsupported':
                self.warn(payload, origin)
            elif kind in ('subfile', 'subfileinclude'):
                self.warn('UNSUPPORTED_SUBFILE', origin, payload)
            elif kind == 'includeonly':
                if any(c in payload for c in '\\#$'):
                    self.warn('DYNAMIC_INCLUDEONLY', origin)
                else:
                    self.includeonly = {posixpath.normpath(s.strip()).removesuffix('.tex') for s in payload.split(',') if s.strip()}
            elif kind in ('input', 'include'):
                dynamic = any(c in payload for c in '\\#$')
                if kind == 'include' and not dynamic and self.includeonly is not None and posixpath.normpath(payload.strip()).removesuffix('.tex') not in self.includeonly:
                    replacement = ''
                else:
                    target = self.resolve(origin, payload, '.tex')
                    if target is not None:
                        if not target.lower().endswith('.tex'):
                            self.warn('NON_TEX_INCLUDE', origin, target)
                        else:
                            expanded = self.expand(target, stack)
                            if expanded is not None: replacement = expanded
            elif kind in ('bibliography', 'addbibresource'):
                for raw in payload.split(','):
                    target = self.resolve(origin, raw, '.bib')
                    if target is not None:
                        if not target.lower().endswith('.bib'):
                            self.warn('NON_BIB_RESOURCE', origin, target)
                            continue
                        try: self.bibs[target] = self.read(target)
                        except (OSError, UnicodeError, ValueError) as exc:
                            if isinstance(exc, GatheringError) and exc.code.endswith('LIMIT'): raise
                            self.warn(getattr(exc, 'code', 'BIB_READ_FAILED'), origin, str(exc))
            prefix = text[position:start]
            size += len(prefix) + len(replacement)
            if size > MAX_OUTPUT_CHARS: raise GatheringError('OUTPUT_SIZE_LIMIT', origin)
            pieces.extend((prefix, replacement)); position = end
        pieces.append(text[position:])
        if size + len(pieces[-1]) > MAX_OUTPUT_CHARS: raise GatheringError('OUTPUT_SIZE_LIMIT', origin)
        return ''.join(pieces)


def gather_source(source: SourceVersion) -> GatheredDocument:
    """Gather one version. included_files is first-visit TeX order, not dedup.

    Root ambiguity is failed. Recoverable dependencies produce partial output
    with original unresolved commands retained and contextual warning codes.
    """
    result = GatheredDocument(source.arxiv_id, source.version, None, '', [], {}, 'failed')
    context = None
    try:
        require_arxiv_id(source.arxiv_id); require_version(source.version)
        if source.download_status == 'skipped' or source.extract_status == 'skipped':
            result.status = 'skipped'
            result.warnings.append(f'{source.arxiv_id}v{source.version} stage=gather M1_STATUS_SKIPPED')
            return result
        if source.download_status != 'ok': raise GatheringError('SOURCE_NOT_DOWNLOADED', source.error_code or source.download_status)
        if source.extract_status == 'empty' and source.error_code == 'NO_TEX_SOURCE':
            if any(str(n).lower().endswith('.tex') for n in source.source_files):
                raise GatheringError('INCONSISTENT_NO_TEX', 'empty manifest lists TeX')
            result.status = 'no_tex'
            return result
        if source.extract_status != 'ok': raise GatheringError('SOURCE_NOT_EXTRACTED', source.error_code or source.extract_status)
        context = Context(source)
        roots = []
        for name in sorted(context.names):
            if name.lower().endswith('.tex'):
                try:
                    count = root_count(context.clean(name))
                    if count > 1: raise GatheringError('MULTIPLE_ROOT_DECLARATIONS', name)
                    if count: roots.append(name)
                except (OSError, UnicodeError, ValueError) as exc:
                    if isinstance(exc, GatheringError): raise
                    raise GatheringError('ROOT_SCAN_INCOMPLETE', name + ': ' + str(exc)) from exc
        if len(roots) != 1:
            raise GatheringError('AMBIGUOUS_ROOT' if roots else 'NO_ROOT', ', '.join(roots))
        context.root = roots[0]
        result.root_file = context.root
        result.latex = context.expand(context.root) or ''
        result.included_files, result.bib_files = context.included, context.bibs
        result.warnings = context.warnings
        result.status = 'partial' if result.warnings else 'ok'
    except (OSError, UnicodeError, ValueError) as exc:
        result.latex = ''
        result.warnings = list(context.warnings) if context else []
        result.warnings.append(f'{source.arxiv_id}v{source.version} stage=gather {getattr(exc, "code", "INVALID_SOURCE")}: {exc}')
    return result
