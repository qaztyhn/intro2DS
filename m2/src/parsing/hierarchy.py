"""Convert gathered LaTeX into an ordered ParsedNode tree.

The gatherer supplies fully expanded LaTeX. This module performs no
filesystem I/O, bibliography extraction, cross-version deduplication or export.
It is a conservative parser: unsupported constructs remain in the text, and
unbalanced structures produce warnings/status='partial'.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .contracts import GatheredDocument, ParsedDocument, ParsedNode
from .normalization import normalize_content, strip_latex_comments

# Python's parser sees only known structural commands; other macros are kept.
_COMMAND = re.compile(r"\\([A-Za-z@]+\*?|.)", re.DOTALL)
_ENV_BEGIN = re.compile(r"\\begin\s*\{([A-Za-z@]+\*?)\}")
_ENV_TAG = re.compile(r"\\(begin|end)\s*\{([A-Za-z@]+\*?)\}")
_PARAGRAPHS = re.compile(r"(?:[ \t]*\n[ \t]*){2,}|\\par(?![A-Za-z])\s*")
_TITLE = re.compile(r"\\title\s*(?:\[[^\]]*\]\s*)?\{")

_HEADINGS = {
    "part": (1, "part"),
    "chapter": (2, "chapter"),
    "section": (3, "section"),
    "subsection": (4, "subsection"),
    "subsubsection": (5, "subsubsection"),
    "paragraph": (6, "paragraph_heading"),
    "subparagraph": (7, "subparagraph_heading"),
}

_MATH_ENVS = {
    "equation", "equation*", "displaymath", "align", "align*",
    "flalign", "flalign*", "alignat", "alignat*", "gather", "gather*",
    "multline", "multline*", "eqnarray", "eqnarray*",
    "math", "aligned", "alignedat", "gathered", "split",
}

_BLOCK_MATH = _MATH_ENVS - {
    "math", "aligned", "alignedat", "gathered", "split"
}

_FIGURE_ENVS = {
    "figure", "figure*", "wrapfigure", "table", "table*", "longtable",
    "tabular", "tabular*", "tabularx", "sidewaystable", "sidewaysfigure",
}

_LIST_ENVS = {"itemize", "enumerate", "description"}

_BIB_ENVS = {"thebibliography", "thebibliography*"}

_ACK_ENVS = {
    "acknowledgements", "acknowledgments",
    "acknowledgement", "acknowledgment"
}

_REFS_HEADINGS = {
    "references", "bibliography", "works cited", "literature cited"
}

_ACK_HEADINGS = {
    "acknowledgements", "acknowledgments",
    "acknowledgement", "acknowledgment"
}

_ABBREVIATIONS = {
    "e.g.", "i.e.", "fig.", "figs.", "eq.", "eqs.",
    "et al.", "dr.", "mr.", "mrs.", "prof.", "vs.", "cf.",
    "sec.", "secs.", "no.", "vol.", "al."
}


@dataclass(frozen=True)
class _Environment:
    name: str
    body: str
    whole: str
    end: int


def _escaped(text: str, pos: int) -> bool:
    count = 0
    pos -= 1

    while pos >= 0 and text[pos] == "\\":
        count += 1
        pos -= 1

    return count % 2 != 0


def _balanced(
    text: str,
    start: int,
    left: str,
    right: str
) -> int | None:
    """Return position after matching bracket, supporting nested groups."""

    if start >= len(text) or text[start] != left:
        return None

    depth = 0

    for i in range(start, len(text)):
        if _escaped(text, i):
            continue

        if text[i] == left:
            depth += 1

        elif text[i] == right:
            depth -= 1

            if depth == 0:
                return i + 1

    return None


def _arg(
    text: str,
    pos: int,
    bracket: str = "{"
) -> tuple[str, int] | None:
    pos = _skip_space(text, pos)
    closing = "}" if bracket == "{" else "]"

    end = _balanced(text, pos, bracket, closing)

    if end is None:
        return None

    return text[pos + 1:end - 1], end


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1

    return i


def _end_of_environment(
    text: str,
    match: re.Match[str]
) -> _Environment | None:
    """Find matching end even when other environments occur inside it."""

    name = match.group(1)
    stack = [name]

    for tag in _ENV_TAG.finditer(text, match.end()):
        if _escaped(text, tag.start()):
            continue

        action, env = tag.groups()

        if action == "begin":
            stack.append(env)

        elif stack[-1] != env:
            return None

        else:
            stack.pop()

            if not stack:
                return _Environment(
                    name,
                    text[match.end():tag.start()],
                    text[match.start():tag.end()],
                    tag.end()
                )

    return None


def _math_end(text: str, start: int, marker: str) -> int:
    i = text.find(marker, start)

    while i >= 0 and _escaped(text, i):
        i = text.find(marker, i + len(marker))

    return i


def _read_heading(
    text: str,
    start: int
) -> tuple[str, int] | None:
    """Consume optional [short title] and mandatory {full title}."""

    i = _skip_space(text, start)

    if i < len(text) and text[i] == "[":
        opt = _arg(text, i, "[")

        if opt is None:
            return None

        i = _skip_space(text, opt[1])

    return _arg(text, i)


def _clean_heading(text: str) -> str:
    # Keep unknown commands instead of guessing their expansion.
    text = re.sub(
        r"\\(?:label|hypertarget)\s*\{[^{}]*\}",
        "",
        text
    )

    return re.sub(r"\s+", " ", text).strip().rstrip(":.").casefold()


def _sentence_parts(text: str) -> list[str]:
    """Split sentences conservatively.

    Avoid splitting inside inline math, LaTeX macro arguments and common
    abbreviations. Keep the trailing fragment even without punctuation.
    """

    result: list[str] = []
    start = 0
    i = 0
    brace_depth = 0
    math_delim: str | None = None

    while i < len(text):

        if math_delim is not None:
            if (
                text.startswith(math_delim, i)
                and not _escaped(text, i)
            ):
                i += len(math_delim)
                math_delim = None
            else:
                i += 1

            continue

        if text.startswith(r"\(", i):
            math_delim = r"\)"
            i += 2
            continue

        if text[i] == "$" and not _escaped(text, i):
            math_delim = "$"
            i += 1
            continue

        if text[i] == "\\" and i + 1 < len(text):
            command = _COMMAND.match(text, i)

            if command:
                i = command.end()
                continue

        if text[i] == "{" and not _escaped(text, i):
            brace_depth += 1

        elif text[i] == "}" and not _escaped(text, i):
            brace_depth = max(0, brace_depth - 1)

        elif brace_depth == 0 and text[i] in ".!?":
            end = i + 1

            if end == len(text) or text[end].isspace():
                before = text[start:end].lower().rstrip()

                if (
                    text[i] == "."
                    and any(
                        before.endswith(abbr)
                        for abbr in _ABBREVIATIONS
                    )
                ):
                    i += 1
                    continue

                fragment = text[start:end].strip()

                if fragment:
                    result.append(fragment)

                start = end

        i += 1

    tail = text[start:].strip()

    if tail:
        result.append(tail)

    return result


class _Builder:
    def __init__(
        self,
        mapping: dict[str, str],
        warnings: list[str]
    ):
        self.mapping = mapping
        self.warnings = warnings
        self.counter = 0
        self.partial = False

    def warn(self, code: str) -> None:
        self.partial = True

        if code not in self.warnings:
            self.warnings.append(code)

    def node(
        self,
        kind: str,
        content: str,
        parent: ParsedNode | None = None
    ) -> ParsedNode:
        node = ParsedNode(
            f"n{self.counter:06d}",
            kind,
            normalize_content(content, self.mapping)
        )

        self.counter += 1

        if parent is not None:
            parent.children.append(node)

        return node

    def paragraphs(
        self,
        raw: str,
        parent: ParsedNode
    ) -> None:
        for piece in _PARAGRAPHS.split(raw):
            cleaned = normalize_content(piece, self.mapping)

            if not cleaned:
                continue

            paragraph = self.node(
                "paragraph",
                cleaned,
                parent
            )

            for sentence in _sentence_parts(cleaned):
                self.node(
                    "sentence",
                    sentence,
                    paragraph
                )

    def list(
        self,
        body: str,
        name: str,
        parent: ParsedNode
    ) -> None:
        lst = self.node("list", "", parent)

        # Remove optional enumitem layout settings.
        cursor = _skip_space(body, 0)

        if cursor < len(body) and body[cursor] == "[":
            options = _arg(body, cursor, "[")

            if options is not None:
                body = body[options[1]:]

        # Parse all list items recursively.
        self.region(
            body,
            lst,
            headings=False,
            items=True
        )

        if not any(
            child.kind == "list_item"
            for child in lst.children
        ):
            self.warn("LIST_WITHOUT_ITEMS")

        # Compose list content from item contents.
        lst.content = normalize_content(
            " ".join(
                child.content
                for child in lst.children
                if child.content
            ),
            self.mapping
        )

    def region(
        self,
        text: str,
        parent: ParsedNode,
        *,
        headings: bool,
        items: bool = False
    ) -> None:
        """Read a region left to right, preserving node order."""

        levels: list[tuple[int, ParsedNode]] = [(0, parent)]
        active = parent
        pending: list[str] = []
        skip_refs: int | None = None
        i = 0

        def flush() -> None:
            if pending:
                raw = "".join(pending)
                pending.clear()

                if skip_refs is None:
                    self.paragraphs(raw, active)

        while i < len(text):

            # Inline math belongs to a sentence.
            if (
                text.startswith(r"\(", i)
                and not _escaped(text, i)
            ):
                end = _math_end(text, i + 2, r"\)")

                if end >= 0:
                    pending.append(text[i:end + 2])
                    i = end + 2
                    continue

            if text[i] == "$" and not _escaped(text, i):

                # $$...$$ is block math.
                if text.startswith("$$", i):
                    end = _math_end(text, i + 2, "$$")

                    if end >= 0:
                        flush()

                        if skip_refs is None:
                            self.node(
                                "equation",
                                text[i:end + 2],
                                active
                            )

                        i = end + 2
                        continue

                    self.warn("UNCLOSED_DISPLAY_MATH")

                # $...$ is inline math.
                else:
                    end = _math_end(text, i + 1, "$")

                    if end >= 0:
                        pending.append(text[i:end + 1])
                        i = end + 1
                        continue

            # \[...\] is block math.
            if (
                text.startswith(r"\[", i)
                and not _escaped(text, i)
            ):
                end = _math_end(text, i + 2, r"\]")

                if end >= 0:
                    flush()

                    if skip_refs is None:
                        self.node(
                            "equation",
                            text[i:end + 2],
                            active
                        )

                    i = end + 2
                    continue

                self.warn("UNCLOSED_DISPLAY_MATH")

            # Detect LaTeX environments.
            env_match = _ENV_BEGIN.match(text, i)

            if env_match and not _escaped(text, i):
                env = _end_of_environment(text, env_match)

                if env is None:
                    self.warn(
                        f"UNCLOSED_ENV:{env_match.group(1)}"
                    )

                else:
                    name = env.name

                    # Skip bibliography.
                    if name in _BIB_ENVS:
                        flush()

                    # Mathematical blocks become equation leaves.
                    elif name in _BLOCK_MATH:
                        flush()

                        if skip_refs is None:
                            self.node(
                                "equation",
                                env.whole,
                                active
                            )

                    # Tables and figures are both figure leaves.
                    elif name in _FIGURE_ENVS:
                        flush()

                        if skip_refs is None:
                            body = env.body

                            if name in {
                                "figure", "figure*",
                                "table", "table*"
                            }:
                                body = re.sub(
                                    r"^\s*\[(?:[htbpH!]+)\]",
                                    "",
                                    body
                                )

                            self.node(
                                "figure",
                                body,
                                active
                            )

                    # Lists have child list_item nodes.
                    elif name in _LIST_ENVS:
                        flush()

                        if skip_refs is None:
                            self.list(
                                env.body,
                                name,
                                active
                            )

                    # Abstract, appendix and acknowledgements.
                    elif name in (
                        {"abstract", "appendix", "appendices"}
                        | _ACK_ENVS
                    ):
                        flush()

                        if skip_refs is None:
                            kind = (
                                "acknowledgements"
                                if name in _ACK_ENVS
                                else "appendix"
                                if name.startswith("appendi")
                                else "abstract"
                            )

                            block = self.node(
                                kind,
                                name.title(),
                                active
                            )

                            self.region(
                                env.body,
                                block,
                                headings=True
                            )

                    # Treat inline math and code environments as text.
                    elif name in {
                        "math",
                        "verbatim",
                        "verbatim*",
                        "lstlisting",
                        "minted"
                    }:
                        pending.append(env.whole)

                    else:
                        # Preserve prose inside unknown wrappers.
                        flush()

                        if skip_refs is None:
                            self.region(
                                env.body,
                                active,
                                headings=False
                            )

                    i = env.end
                    continue

            # Detect LaTeX commands.
            command = _COMMAND.match(text, i)

            if command and not _escaped(text, i):
                full_name = command.group(1)
                name = full_name.rstrip("*")

                # Heading commands such as \section and \subsection.
                if headings and name in _HEADINGS:
                    arg = _read_heading(
                        text,
                        command.end()
                    )

                    if arg is None:
                        self.warn(f"INVALID_HEADING:{name}")

                    else:
                        title, end = arg
                        normalized = normalize_content(
                            title,
                            self.mapping
                        )

                        lvl, kind = _HEADINGS[name]

                        if skip_refs is not None:
                            if lvl > skip_refs:
                                i = end
                                continue

                            # Discard references text before resuming.
                            flush()
                            skip_refs = None

                        else:
                            flush()

                        classification = _clean_heading(
                            normalized
                        )

                        if classification in _REFS_HEADINGS:
                            skip_refs = lvl

                        else:
                            if classification in _ACK_HEADINGS:
                                kind = "acknowledgements"

                            elif classification.startswith(
                                ("appendix", "appendices")
                            ):
                                kind = "appendix"

                            # Find the correct parent by heading level.
                            while levels[-1][0] >= lvl:
                                levels.pop()

                            active = self.node(
                                kind,
                                title,
                                levels[-1][1]
                            )

                            levels.append((lvl, active))

                        i = end
                        continue

                # \appendix starts an appendix region.
                if headings and name == "appendix":
                    flush()
                    skip_refs = None
                    levels = [(0, parent)]

                    active = self.node(
                        "appendix",
                        "Appendix",
                        parent
                    )

                    levels.append((1, active))
                    i = command.end()
                    continue

                # Acknowledgements commands.
                if headings and name in _ACK_ENVS:
                    flush()
                    skip_refs = None
                    levels = [(0, parent)]

                    active = self.node(
                        "acknowledgements",
                        "Acknowledgements",
                        parent
                    )

                    levels.append((3, active))
                    i = command.end()
                    continue

                # \item starts a list item.
                if items and name == "item":
                    flush()
                    label = ""

                    cursor = _skip_space(
                        text,
                        command.end()
                    )

                    if (
                        cursor < len(text)
                        and text[cursor] == "["
                    ):
                        opt = _arg(text, cursor, "[")

                        if opt is not None:
                            label, cursor = opt

                    active = self.node(
                        "list_item",
                        label,
                        parent
                    )

                    i = (
                        cursor
                        if cursor != _skip_space(
                            text,
                            command.end()
                        )
                        else command.end()
                    )

                    continue

                # Skip bibliography import commands.
                if name in {
                    "bibliography",
                    "bibliographystyle",
                    "printbibliography"
                }:
                    flush()

                    cursor = _skip_space(
                        text,
                        command.end()
                    )

                    if (
                        cursor < len(text)
                        and text[cursor] == "["
                    ):
                        opt = _arg(text, cursor, "[")

                        if opt:
                            cursor = opt[1]

                    if (
                        cursor < len(text)
                        and text[cursor] == "{"
                    ):
                        a = _arg(text, cursor)

                        if a:
                            cursor = a[1]

                    i = cursor
                    continue

                if name == "bibitem":
                    flush()

                    if skip_refs is None:
                        self.warn(
                            "BIBITEM_OUTSIDE_BIBLIOGRAPHY"
                        )
                        skip_refs = 0

                    i = command.end()
                    continue

                # TV1 should already resolve includes.
                if name in {
                    "input", "include", "subfile",
                    "import", "subimport"
                }:
                    if skip_refs is None:
                        self.warn("UNRESOLVED_INCLUDE")

                    # Keep command and argument for diagnosis.

                # Ignore non-content commands.
                if name in {
                    "maketitle",
                    "tableofcontents",
                    "listoffigures",
                    "listoftables"
                }:
                    i = command.end()
                    continue

                if name == "par":
                    flush()
                    i = command.end()
                    continue

            # Preserve unknown commands and their arguments.
            if command and not _escaped(text, i):
                j = command.end()

                while True:
                    k = _skip_space(text, j)

                    if (
                        k < len(text)
                        and text[k] in "{["
                    ):
                        a = _arg(
                            text,
                            k,
                            text[k]
                        )

                        if a is None:
                            break

                        j = a[1]

                    else:
                        break

                pending.append(text[i:j])
                i = j
                continue

            pending.append(text[i])
            i += 1

        flush()

        if items:
            for item in parent.children:
                if item.kind == "list_item":
                    # Include child content in list_item content.
                    direct_text = " ".join(
                        c.content
                        for c in item.children
                        if c.content
                    )

                    if direct_text:
                        item.content = normalize_content(
                            (
                                item.content
                                + " "
                                + direct_text
                            ).strip(),
                            self.mapping
                        )


def _document_body(
    latex: str,
    warnings: list[str]
) -> str:
    start = re.search(
        r"\\begin\s*\{document\}",
        latex
    )

    if not start:
        warnings.append("DOCUMENT_ENV_MISSING")

        # Some sources are snippets instead of full documents.
        return latex

    end = re.search(
        r"\\end\s*\{document\}",
        latex[start.end():]
    )

    if end is None:
        warnings.append("DOCUMENT_END_MISSING")
        return latex[start.end():]

    return latex[
        start.end():
        start.end() + end.start()
    ]


def _document_title(
    latex: str,
    mapping: dict[str, str]
) -> str:
    match = _TITLE.search(latex)

    if match:
        arg = _arg(
            latex,
            match.end() - 1
        )

        if arg is not None:
            title = normalize_content(
                arg[0],
                mapping
            )

            if title:
                return title

    return "Document"


def parse_hierarchy(
    document: GatheredDocument,
    citation_mapping: dict[str, str],
) -> ParsedDocument:
    """Parse one gathered arXiv version using the M2 TV2 contract.

    Local IDs are deterministic within each document.
    TV4 is responsible for final cross-version IDs and deduplication.
    """

    warnings = list(document.warnings)

    if document.status not in {"ok", "partial"}:
        return ParsedDocument(
            document.arxiv_id,
            document.version,
            None,
            document.status,
            warnings
        )

    if not document.latex.strip():
        return ParsedDocument(
            document.arxiv_id,
            document.version,
            None,
            "no_tex",
            warnings + ["EMPTY_LATEX"]
        )

    # Remove source comments before structural parsing.
    source = strip_latex_comments(document.latex)

    # Extract content inside \begin{document}...\end{document}.
    body = _document_body(source, warnings)

    builder = _Builder(
        citation_mapping,
        warnings
    )

    # Create document root.
    root = builder.node(
        "document",
        _document_title(source, citation_mapping)
    )

    # Parse all child nodes.
    builder.region(
        body,
        root,
        headings=True
    )

    # Determine final parse status.
    status = (
        "partial"
        if (
            document.status == "partial"
            or builder.partial
            or "DOCUMENT_ENV_MISSING" in warnings
            or "DOCUMENT_END_MISSING" in warnings
        )
        else "ok"
    )

    return ParsedDocument(
        document.arxiv_id,
        document.version,
        root,
        status,
        warnings
    )