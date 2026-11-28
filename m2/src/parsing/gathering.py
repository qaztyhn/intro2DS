"""TV1: bounded literal LaTeX gathering. No compilation, writes or network.

This is not a TeX macro interpreter. Unknown conditions/dynamic dependencies
remain visible with partial status, instead of pretending to follow both paths.
"""
from __future__ import annotations

import posixpath
import re
import json
from pathlib import Path, PurePosixPath, PureWindowsPath

from m1.src.schema import no_symlinks, relative_path, require_arxiv_id, require_version
from m1.src.compilation import MAX_COMPILATION_BYTES, compilation_record
from .contracts import GatheredDocument, SourceVersion

MAX_FILE_BYTES = 64 * 2**20
MAX_TOTAL_BYTES = 512 * 2**20
MAX_OUTPUT_CHARS = 128 * 2**20
MAX_DEPTH = 128
LITERALS = {'verbatim', 'verbatim*', 'Verbatim', 'lstlisting', 'minted', 'comment',
            'vquote', 'filecontents', 'filecontents*'}
DEFINITIONS = {'newcommand', 'renewcommand', 'providecommand', 'DeclareRobustCommand',
               'DeclareMathOperator', 'def', 'gdef', 'edef', 'xdef', 'newenvironment', 'renewenvironment'}
DIRECTIVES = re.compile(r'\\(?:input|include|import|subimport|bibliography|addbibresource|documentclass|documentstyle)(?![A-Za-z@])')
COMMAND = re.compile(r'\\([A-Za-z@]+|[^\n])')
SPECIAL = re.compile(r'[\\%]')
CONDITIONAL_MACROS = {'ifthenelse': 3, 'ifdef': 3, 'ifboolexpr': 3,
                      'ifstrequal': 4, 'IfFileExists': 3, 'ifdraft': 2,
                      'ifcsdef': 3, 'ifcsundef': 3, 'ifdefempty': 3,
                      'ifblank': 3, 'ifstrempty': 3, 'ifcsempty': 3}


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


def defined_command(text, end):
    start = spaces(text, end)
    if start < len(text) and text[start] == '*': start = spaces(text, start + 1)
    target = group(text, start)
    token = target[0].strip() if target else text[start:]
    match = COMMAND.match(token)
    return match[1] if match else None


def conditional(text, end, non_conditionals=(), conditional_flags=()):
    depth, index, otherwise = 1, end, None
    ordinary = set(non_conditionals)
    flags = set(conditional_flags)
    while index < len(text):
        if text[index] != '\\': index += 1; continue
        special = literal(text, index)
        if special: index = special[1]; continue
        name, after = command(text, index)
        if name == 'newif':
            target = spaces(text, after)
            flag, index = command(text, target) if target < len(text) and text[target] == '\\' else ('', after)
            flags.add(flag)
            ordinary.discard(flag)
            continue
        if name in DEFINITIONS:
            body = definition(text, name, after)
            if body:
                if name not in ('newenvironment', 'renewenvironment'):
                    ordinary.add(defined_command(text, after))
                index = body[1]; continue
        if name in CONDITIONAL_MACROS and name not in ordinary and name not in flags:
            # Package conditionals use arguments, not a matching primitive \fi.
            for _ in range(CONDITIONAL_MACROS[name]):
                value = group(text, after)
                if value: after = value[1]
                elif text[spaces(text, after):].startswith('\\'):
                    _, after = command(text, spaces(text, after))
            index = after
            continue
        if (is_conditional(name) or name in flags) and name not in ordinary:
            depth += 1
        elif name == 'fi':
            depth -= 1
            if depth == 0:
                return (text[end:otherwise[0]] if otherwise else text[end:index],
                        text[otherwise[1]:index] if otherwise else '', after)
        elif name == 'else' and depth == 1: otherwise = (index, after)
        index = after
    return None


def events(text, non_conditionals=None, conditional_flags=None, flag_values=None, flag_scopes=None):
    """Yield nonoverlapping (start, end, kind, payload) lexical events."""
    index = 0
    ordinary = set() if non_conditionals is None else non_conditionals
    flags = set() if conditional_flags is None else conditional_flags
    values = {} if flag_values is None else flag_values
    scopes = [] if flag_scopes is None else flag_scopes
    global_assignment = False
    while index < len(text):
        if text[index] != '\\':
            if text[index] == '{':
                scopes.append(dict(values))
            elif text[index] == '}' and scopes:
                values.clear(); values.update(scopes.pop())
            index += 1
            continue
        start = index
        special = literal(text, start)
        if special: index = special[1]; continue
        name, end = command(text, start)
        index = end
        if name == 'global':
            global_assignment = True
            continue
        if name == 'begingroup':
            scopes.append(dict(values))
            continue
        if name == 'endgroup':
            if scopes:
                values.clear(); values.update(scopes.pop())
            continue
        if name == 'newif':
            target = spaces(text, end)
            if target < len(text) and text[target] == '\\':
                flag, index = command(text, target)
                flags.add(flag)
                ordinary.discard(flag)
                values[flag] = False
            continue
        setter = ('if' + name[:-4], True) if name.endswith('true') else (
            ('if' + name[:-5], False) if name.endswith('false') else None)
        if setter and setter[0] in flags and name not in ordinary:
            values[setter[0]] = setter[1]
            if global_assignment:
                for scope in scopes:
                    scope[setter[0]] = setter[1]
            global_assignment = False
            continue
        global_assignment = False
        if name in DEFINITIONS:
            body = definition(text, name, end)
            if body:
                index = body[1]
                if name not in ('newenvironment', 'renewenvironment'):
                    defined = defined_command(text, end)
                    ordinary.add(defined)
                    if defined in flags:
                        values[defined] = None
                    for suffix in ('true', 'false'):
                        if defined and defined.endswith(suffix):
                            flag = 'if' + defined[:-len(suffix)]
                            if flag in flags:
                                values[flag] = None
                yield start, index, 'definition', body[0]
            else:
                yield start, end, 'unsupported', 'MALFORMED_MACRO_DEFINITION'
            continue
        if (is_conditional(name) or name in flags) and name not in ordinary:
            branches = conditional(text, end, ordinary, flags)
            if branches:
                yes, no, index = branches
                known = True if name == 'iftrue' else False if name == 'iffalse' else values.get(name)
                payload = yes if known is True else no if known is False else None
                if payload is None:
                    for flag in values:
                        values[flag] = None
                yield start, index, 'conditional', payload
            else:
                index = len(text)
                yield start, index, 'unsupported', 'UNTERMINATED_CONDITIONAL'
            continue
        if name == 'unless':
            after = spaces(text, end)
            _, condition_end = command(text, after) if after < len(text) and text[after] == '\\' else ('', after)
            branches = conditional(text, condition_end, ordinary, flags)
            index = branches[2] if branches else len(text)
            for flag in values:
                values[flag] = None
            yield start, index, 'conditional', None
            continue
        if name in CONDITIONAL_MACROS and name not in ordinary and name not in flags:
            for argument in range(CONDITIONAL_MACROS[name]):
                value = group(text, index)
                if value: index = value[1]
                elif argument == 0:
                    token = spaces(text, index)
                    if token < len(text) and text[token] == '\\': _, index = command(text, token)
            for flag in values:
                values[flag] = None
            yield start, index, 'conditional', None
            continue
        if name in ('documentclass', 'documentstyle'):
            yield start, end, 'root', None
        elif name == 'printbibliography':
            yield start, end, 'printbibliography', None
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
                match = re.match(r'[^\s{}\\]+', text[after:])
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
        elif name == 'end':
            value = group(text, end)
            if value and value[0].strip() == 'document':
                # TeX terminates the job here. Submitted sources sometimes
                # append another template/example document after this point.
                index = len(text)
                yield start, index, 'enddocument', text[start:value[1]]


def root_count(text, depth=0, non_conditionals=None, conditional_flags=None, flag_values=None, flag_scopes=None):
    if depth > MAX_DEPTH: raise GatheringError('NESTING_LIMIT', 'conditional nesting exceeds limit')
    count = 0
    ordinary = set() if non_conditionals is None else non_conditionals
    flags = set() if conditional_flags is None else conditional_flags
    values = {} if flag_values is None else flag_values
    scopes = [] if flag_scopes is None else flag_scopes
    for _, _, kind, payload in events(text, ordinary, flags, values, scopes):
        if kind == 'root': count += 1
        elif kind == 'conditional' and payload is not None:
            count += root_count(payload, depth + 1, ordinary, flags, values, scopes)
    return count


def _path_candidates(names, bases, raw, suffix):
    """Prefer an exact declared name; only then try the TeX/BibTeX suffix.

    A dot in a basename (01.introduction) is not proof of an explicit suffix.
    Multiple matches at either stage remain ambiguous.
    """
    paths = {posixpath.normpath(posixpath.join(base, raw)) for base in bases}
    paths = {p for p in paths if p != '..' and not p.startswith('../')}
    exact = paths & names
    if exact:
        return exact, bool(paths)
    appended = {p + suffix for p in paths if not p.lower().endswith(suffix)}
    return appended & names, bool(paths)


def _declared_tex_dependency(context, origin, raw):
    """Resolve one static TeX dependency without guessing or warnings."""
    raw = raw.strip()
    if not raw or any(c in raw for c in '\\#$\x00{}'):
        return None
    if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).drive or ':' in raw:
        return None
    candidates, _ = _path_candidates(context.names, (posixpath.dirname(origin), ''), raw, '.tex')
    return next(iter(candidates)) if len(candidates) == 1 else None


def _top_level_roots(context, roots):
    """Remove root candidates explicitly included by another root.

    Only a unique dependency-graph root is selected automatically. Independent
    documents remain ambiguous and require a reviewed root choice.
    """
    root_set = set(roots)
    referenced = set()
    for origin in roots:
        for _, _, kind, payload in events(context.clean(origin)):
            if kind not in ('input', 'include', 'subfile', 'subfileinclude') or payload is None:
                continue
            target = _declared_tex_dependency(context, origin, payload)
            if kind in ('subfile', 'subfileinclude') and target is not None:
                try:
                    _subfile_parts(context.clean(target))
                except ValueError:
                    continue
            if target in root_set and target != origin:
                referenced.add(target)
    top_level = [name for name in roots if name not in referenced]
    return top_level if len(top_level) == 1 else roots


def _compilation_hint(context):
    """Read only fixed recovery sidecars and rederive their content from README."""
    sidecar = context.base.parent / 'recovery_compilation.json'
    if not sidecar.exists() and not sidecar.is_symlink():
        return None
    readme = context.base.parent / 'original_assets' / '00README.json'
    try:
        for path in (sidecar, readme):
            no_symlinks(path)
        with sidecar.open('rb') as handle:
            raw_sidecar = handle.read(MAX_COMPILATION_BYTES + 1)
        if len(raw_sidecar) > MAX_COMPILATION_BYTES:
            raise ValueError('compilation sidecar exceeds size limit')
        with readme.open('rb') as handle:
            raw_readme = handle.read(MAX_COMPILATION_BYTES + 1)
        expected = compilation_record(raw_readme, context.source.arxiv_id, context.source.version)
        supplied = json.loads(raw_sidecar.decode('utf-8'))
        if (not isinstance(supplied, dict) or type(supplied.get('version')) is not int
                or type(supplied.get('schema_version')) is not int or supplied != expected):
            raise ValueError('compilation sidecar differs from original README or identity')
        return expected
    except (OSError, UnicodeError, ValueError) as exc:
        context.warn('INVALID_COMPILATION_METADATA', detail=str(exc))
        return None


def _subfile_parts(text):
    """Recognize a literal subfiles document, excluding definitions and literals."""
    index, depth = 0, 0
    classes, begins, ends = [], [], []
    while index < len(text):
        if text[index] != '\\':
            if text[index] == '{': depth += 1
            elif text[index] == '}': depth -= 1
            index += 1
            continue
        start = index
        special = literal(text, start)
        if special:
            index = special[1]
            continue
        name, index = command(text, start)
        if name in DEFINITIONS:
            body = definition(text, name, index)
            if body:
                index = body[1]
                continue
        if is_conditional(name):
            branches = conditional(text, index)
            if branches:
                if re.search(r'\\(?:begin|end)\s*\{document\}', branches[0] + branches[1]):
                    raise ValueError('conditional document envelope is unsupported')
                index = branches[2]
                continue
        if depth != 0:
            continue
        if name == 'documentclass':
            optional = group(text, index, '[', ']')
            value = group(text, optional[1] if optional else index)
            if value:
                classes.append(value[0].strip())
                index = value[1]
        elif name in ('begin', 'end'):
            value = group(text, index)
            if value and value[0].strip() == 'document':
                (begins if name == 'begin' else ends).append((start, value[1]))
                index = value[1]
    if classes != ['subfiles'] or len(begins) != 1 or len(ends) != 1 or begins[0][1] > ends[0][0]:
        raise ValueError('expected one literal documentclass{subfiles} and document envelope')
    return text[:begins[0][0]], text[begins[0][1]:ends[0][0]]


class Context:
    def __init__(self, source):
        self.source = source
        self.base = no_symlinks(Path(source.source_dir).absolute())
        if not self.base.is_dir(): raise GatheringError('MISSING_SOURCE_DIRECTORY', str(self.base))
        self.names = set()
        for name in source.source_files:
            relative = relative_path(name)
            if PurePosixPath(relative).suffix.lower() not in ('.tex', '.bib', '.bbl'):
                raise GatheringError('INVALID_SOURCE_TYPE', relative)
            if relative in self.names: raise GatheringError('DUPLICATE_SOURCE_PATH', relative)
            self.names.add(relative)
        self.cache, self.clean_cache = {}, {}
        self.bytes_read = 0
        self.warnings, self.included, self.bibs = [], [], {}
        self.pending_warnings = {}
        self.non_conditionals = set()
        self.conditional_flags = set()
        self.flag_values = {}
        self.flag_scopes = []
        self.root, self.includeonly = None, None
        self.printbibliography_active = False

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
        bases = (posixpath.dirname(origin), posixpath.dirname(self.root or ''), '')
        candidates, safe = _path_candidates(self.names, bases, raw, suffix)
        if not candidates:
            missing_code = 'MISSING_BIB_RESOURCE' if suffix == '.bib' else 'MISSING_DEPENDENCY'
            self.warn(missing_code if safe else 'UNSAFE_PATH', origin, raw)
            return None
        if len(candidates) > 1:
            if self.equivalent_bbl_resources(candidates):
                preferred = posixpath.normpath(posixpath.join(posixpath.dirname(origin), raw))
                return preferred if preferred in candidates else sorted(candidates)[0]
            self.warn('AMBIGUOUS_PATH', origin, raw + ' -> ' + ', '.join(sorted(candidates)))
            return None
        return next(iter(candidates))

    def equivalent_bbl_resources(self, paths):
        if not all(p.lower().endswith(('.bbl', '.bbl.tex', '.bbl.bib')) for p in paths):
            return False
        return len({self.read(p) for p in paths}) == 1

    def _load_bbl(self, path, origin):
        """Read declared raw/derived BBL; preserve normal resource limits."""
        if path in self.bibs:
            return
        try:
            self.bibs[path] = self.read(path)
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, GatheringError) and exc.code.endswith('LIMIT'):
                raise
            self.warn(getattr(exc, 'code', 'BBL_READ_FAILED'), origin, str(exc))

    def attach_root_bbl(self):
        """Attach the compilation root's .bbl, not arbitrary unused files."""
        root_path = PurePosixPath(self.root)
        candidates = {str(root_path.with_suffix('.bbl'))}
        if root_path.parent != PurePosixPath('.'):
            candidates.add(root_path.stem + '.bbl')
        if self.printbibliography_active:
            candidates |= {p + suffix for p in tuple(candidates) for suffix in ('.bib', '.tex')}
        found = sorted(candidates & self.names)
        if len(found) > 1:
            if self.equivalent_bbl_resources(found):
                preferred = str(root_path.with_suffix('.bbl'))
                self._load_bbl(preferred if preferred in found else found[0], self.root)
            else:
                self.warn('AMBIGUOUS_BBL_RESOURCE', self.root, ', '.join(found))
        elif found:
            self._load_bbl(found[0], self.root)

    def expand(self, name, stack=(), *, subfile=False):
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
        if subfile:
            try:
                preamble, text = _subfile_parts(text)
            except ValueError as exc:
                self.warn('UNSUPPORTED_SUBFILE', name, str(exc))
                return None
            self._subfile_bibliography(preamble, name)
        return self.expand_text(text, name, (*stack, name))

    def _bibliography_resources(self, origin, payload):
        for raw in payload.split(','):
            target = self.resolve(origin, raw, '.bib')
            if target is None:
                continue
            if not target.lower().endswith('.bib'):
                self.warn('NON_BIB_RESOURCE', origin, target)
                continue
            try:
                self.bibs[target] = self.read(target)
            except (OSError, UnicodeError, ValueError) as exc:
                if isinstance(exc, GatheringError) and exc.code.endswith('LIMIT'): raise
                self.warn(getattr(exc, 'code', 'BIB_READ_FAILED'), origin, str(exc))

    def _subfile_bibliography(self, preamble, origin, depth=0):
        if depth > MAX_DEPTH:
            raise GatheringError('NESTING_LIMIT', origin)
        for _, _, kind, payload in events(preamble):
            if kind in ('bibliography', 'addbibresource'):
                self._bibliography_resources(origin, payload)
            elif kind == 'conditional' and payload is not None:
                self._subfile_bibliography(payload, origin, depth + 1)

    def expand_text(self, text, origin, stack, depth=0):
        if depth > MAX_DEPTH: raise GatheringError('NESTING_LIMIT', origin)
        pieces, position, size = [], 0, 0
        for start, end, kind, payload in events(text, self.non_conditionals, self.conditional_flags, self.flag_values, self.flag_scopes):
            replacement = text[start:end]
            if kind == 'definition':
                if DIRECTIVES.search(payload): self.warn('DEFERRED_MACRO_DIRECTIVE', origin)
            elif kind == 'conditional':
                if payload is None: self.warn('UNRESOLVED_CONDITIONAL', origin)
                else: replacement = self.expand_text(payload, origin, stack, depth + 1)
            elif kind == 'endinput':
                replacement = payload
                if payload.strip(): self.warn('ENDINPUT_TRAILING_TOKENS', origin, 'same-line tokens left unexpanded')
            elif kind == 'enddocument':
                replacement = payload
            elif kind == 'unsupported':
                self.warn(payload, origin)
            elif kind in ('subfile', 'subfileinclude'):
                if kind in self.non_conditionals:
                    self.warn('UNSUPPORTED_SUBFILE', origin, 'command redefined by source')
                    continue
                target = self.resolve(origin, payload, '.tex')
                if target is not None:
                    expanded = self.expand(target, stack, subfile=True)
                    if expanded is not None: replacement = expanded
            elif kind == 'printbibliography':
                self.printbibliography_active = True
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
                        if target.lower().endswith(('.bbl', '.bbl.tex')):
                            # Reference entries are extracted separately, not
                            # inserted into the document hierarchy.
                            self._load_bbl(target, origin)
                            replacement = ''
                        elif not target.lower().endswith('.tex'):
                            self.warn('NON_TEX_INCLUDE', origin, target)
                        else:
                            expanded = self.expand(target, stack)
                            if expanded is not None: replacement = expanded
            elif kind in ('bibliography', 'addbibresource'):
                self._bibliography_resources(origin, payload)
            prefix = text[position:start]
            size += len(prefix) + len(replacement)
            if size > MAX_OUTPUT_CHARS: raise GatheringError('OUTPUT_SIZE_LIMIT', origin)
            pieces.extend((prefix, replacement)); position = end
        pieces.append(text[position:])
        if size + len(pieces[-1]) > MAX_OUTPUT_CHARS: raise GatheringError('OUTPUT_SIZE_LIMIT', origin)
        return ''.join(pieces)


def _gather_source(source: SourceVersion, selected_root=None) -> GatheredDocument:
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
        original_roots = roots
        compilation_hint = _compilation_hint(context)
        hinted_root = None
        if compilation_hint and len(compilation_hint['toplevels']) == 1:
            candidate = compilation_hint['toplevels'][0]
            if candidate in roots:
                hinted_root = candidate
        if selected_root is not None:
            # The choice must still be an active, manifest-listed root. Never
            # read an arbitrary path or silently accept a stale configuration.
            selected = relative_path(selected_root)
            if selected not in roots:
                raise GatheringError('INVALID_ROOT_SELECTION', selected)
            context.root = selected
            context.warn('ROOT_SELECTED_BY_CONFIG', selected,
                         'candidates=' + ', '.join(roots))
        else:
            if hinted_root is not None:
                roots = [hinted_root]
            elif len(roots) > 1 and not (compilation_hint and len(compilation_hint['toplevels']) > 1):
                roots = _top_level_roots(context, roots)
        if selected_root is None and len(roots) != 1:
            raise GatheringError('AMBIGUOUS_ROOT' if roots else 'NO_ROOT', ', '.join(roots))
        elif selected_root is None:
            context.root = roots[0]
            if len(original_roots) > 1 and hinted_root is None:
                context.warn('ROOT_SELECTED_BY_DEPENDENCY_GRAPH', context.root,
                             'candidates=' + ', '.join(original_roots))
        result.root_file = context.root
        result.latex = context.expand(context.root) or ''
        context.attach_root_bbl()
        result.included_files, result.bib_files = context.included, context.bibs
        result.warnings = context.warnings
        result.status = 'partial' if result.warnings else 'ok'
    except (OSError, UnicodeError, ValueError) as exc:
        result.latex = ''
        result.warnings = list(context.warnings) if context else []
        result.warnings.append(f'{source.arxiv_id}v{source.version} stage=gather {getattr(exc, "code", "INVALID_SOURCE")}: {exc}')
    return result


def gather_source(source: SourceVersion) -> GatheredDocument:
    """Contract interface: ambiguous roots fail unless a separate choice is supplied."""
    return _gather_source(source)


def gather_source_with_root(source: SourceVersion, root_file: str) -> GatheredDocument:
    """Gather a reviewed root choice without changing the team's public interface."""
    return _gather_source(source, selected_root=root_file)
