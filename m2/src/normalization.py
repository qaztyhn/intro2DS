
"""Non-destructive LaTeX content normalization for Intro2DS Milestone 2.

This module works on CONTENT of individual parsed nodes, not on a complete
LaTeX document before structural parsing. It performs no file I/O and does not
resolve imports, bibliography entries, or cross-version deduplication.

Unsupported macros/environments are deliberately preserved rather than erased.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Cosmetic macros with one mandatory argument that can be unwrapped without
# discarding their visible text. Do not put semantic commands here.
_FORMATTING = frozenset({
    "textbf", "textit", "texttt", "textrm", "textsf", "textsc",
    "textnormal", "emph", "underline", "mbox",
})
_FORMAT_COMMAND = re.compile(
    r"\\(" + "|".join(sorted(_FORMATTING, key=len, reverse=True))
    + r")\s*(?=\{)"
)
# Zero-argument layout-only commands. Preserve \label, \ref, \caption,
# \includegraphics and unknown commands because they can carry information.
_LAYOUT = re.compile(
    r"\\(?:centering|raggedright|raggedleft|noindent|"
    r"toprule|midrule|bottomrule|hline|smallskip|medskip|bigskip)"
    r"(?![A-Za-z])\s*"
)
_CITATION_COMMANDS = (
    "citeyearpar", "citeauthor", "supercite", "parencite", "autocite",
    "textcite", "footcite", "citealp", "citealt", "citeyear",
    "citet", "citep", "nocite", "cite", "Citet", "Citep", "Cite",
)
_CITATION = re.compile(
    r"\\(?:" + "|".join(_CITATION_COMMANDS) + r")\*?(?![A-Za-z])"
)
# Display-math environments recognized by the M2 normalizer. Multi-row
# environments retain their environment type (and optional *), because an
# equation wrapper would change per-row numbering and label semantics.
_DISPLAY_ENVS = frozenset({
    "equation", "equation*", "displaymath", "align", "align*",
    "flalign", "flalign*", "alignat", "alignat*", "gather", "gather*",
    "multline", "multline*", "eqnarray", "eqnarray*",
})
_ENV_TAG = re.compile(r"\\(begin|end)\s*\{([A-Za-z]+\*?)\}") 
_DISPLAY_START = re.compile(
    r"\$\$|\\\[|\\begin\s*\{(?:"
    + "|".join(re.escape(name) for name in sorted(_DISPLAY_ENVS, key=len, reverse=True))
    + r")\}"
)
_INLINE_MATH_ENV = re.compile(r"\\begin\s*\{math\}")
_MATH_TEXT_ARG = re.compile(
    r"\\(?:text|mbox|textrm|textsf|texttt|textnormal|operatorname\*?)\s*(?=\{)"
)

_VERBATIM_ENV_START = re.compile(r"\\begin\{(verbatim\*?|lstlisting|minted)\}")


def _matching_group_end(text: str, start: int, opening: str, closing: str) -> int | None:
    """Return exclusive end of a balanced group; ignore escaped delimiters."""
    if start >= len(text) or text[start] != opening:
        return None
    depth = 0
    i = start
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            # A command beginning with backslash doesn't escape all future
            # braces; only skip the immediately following character.
            i += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _is_escaped(text: str, pos: int) -> bool:
    slashes = 0
    pos -= 1
    while pos >= 0 and text[pos] == "\\":
        slashes += 1
        pos -= 1
    return slashes % 2 == 1


def _protect_verbatim(content: str) -> tuple[str, dict[str, str]]:
    """Shield code-like spans so cleanup cannot change their literal content."""
    prefix = "__M2_VERBATIM_"
    while prefix in content:
        prefix = "_" + prefix
    chunks: list[str] = []
    protected: dict[str, str] = {}
    i = 0
    while i < len(content):
        end = -1
        match = _VERBATIM_ENV_START.match(content, i)
        if match:
            env = match.group(1)
            end_marker = rf"\end{{{env}}}"
            closing = content.find(end_marker, match.end())
            if closing >= 0:
                end = closing + len(end_marker)

        if end < 0 and content.startswith(r"\verb", i) and not _is_escaped(content, i):
            arg_start = i + len(r"\verb")
            if arg_start < len(content) and content[arg_start] == "*":
                arg_start += 1
            if arg_start < len(content):
                delimiter = content[arg_start]
                if not (delimiter.isalpha() or delimiter.isspace()):
                    closing = content.find(delimiter, arg_start + 1)
                    if closing >= 0:
                        end = closing + 1

        if end >= 0:
            marker = f"{prefix}{len(protected)}__"
            protected[marker] = content[i:end]
            chunks.append(marker)
            i = end
        else:
            chunks.append(content[i])
            i += 1
    return "".join(chunks), protected


def _restore_verbatim(content: str, protected: dict[str, str]) -> str:
    for marker, original in protected.items():
        content = content.replace(marker, original)
    return content


def strip_latex_comments(content: str) -> str:
    r"""Remove unescaped % comments while retaining escaped \% and newlines.

    Verbatim-like environments and inline \verb are preserved here. Generic
    normalization is intended for semantic nodes; a full code-listing node
    should not be passed through whitespace/formatting normalization.
    """
    output: list[str] = []
    i = 0
    while i < len(content):
        env_match = _VERBATIM_ENV_START.match(content, i)
        if env_match:
            env = env_match.group(1)
            end_marker = rf"\end{{{env}}}"
            end = content.find(end_marker, env_match.end())
            if end >= 0:
                finish = end + len(end_marker)
                output.append(content[i:finish])
                i = finish
                continue

        if content.startswith(r"\verb", i) and not _is_escaped(content, i):
            end_cmd = i + 5
            if end_cmd < len(content) and content[end_cmd] == "*":
                end_cmd += 1
            if end_cmd < len(content):
                delimiter = content[end_cmd]
                if not (delimiter.isalpha() or delimiter.isspace()):
                    close = content.find(delimiter, end_cmd + 1)
                    if close >= 0:
                        output.append(content[i:close + 1])
                        i = close + 1
                        continue

        if content[i] == "%" and not _is_escaped(content, i):
            newline = content.find("\n", i)
            if newline < 0:
                break
            output.append("\n")
            i = newline + 1
            continue
        output.append(content[i])
        i += 1
    return "".join(output)


def normalize_citations(content: str, citation_mapping: Mapping[str, str] | None = None) -> str:
    """Map cite keys to canonical keys, preserving command/options/order.

    Supports optional square-bracket arguments and simple multi-key commands.
    Unknown keys are left intact; duplicate canonical keys within a single
    command are emitted once in first-seen order. Bibliography parsing and key
    collision resolution belong to TV3, not to this function.
    """
    mapping = citation_mapping or {}
    result: list[str] = []
    pos = 0
    for match in _CITATION.finditer(content):
        if match.start() < pos or _is_escaped(content, match.start()):
            continue
        cursor = match.end()
        while cursor < len(content) and content[cursor].isspace():
            cursor += 1
        while cursor < len(content) and content[cursor] == "[":
            end = _matching_group_end(content, cursor, "[", "]")
            if end is None:
                break
            cursor = end
            while cursor < len(content) and content[cursor].isspace():
                cursor += 1
        if cursor >= len(content) or content[cursor] != "{":
            continue
        end = _matching_group_end(content, cursor, "{", "}")
        if end is None:
            continue
        original_keys = content[cursor + 1:end - 1].split(",")
        canonical: list[str] = []
        seen: set[str] = set()
        for key in original_keys:
            clean_key = key.strip()
            if not clean_key:
                continue
            mapped = mapping.get(clean_key, clean_key)
            if mapped not in seen:
                canonical.append(mapped)
                seen.add(mapped)
        # Preserve \cite[notes]{...} and its exact options. Only the keys
        # inside the mandatory group are changed.
        result.append(content[pos:cursor + 1])
        result.append(",".join(canonical))
        result.append("}")
        pos = end
    result.append(content[pos:])
    return "".join(result)


def normalize_inline_math(content: str) -> str:
    r"""Canonicalize $...$ and \\(...\\) into $normalized expression$.

    Escape-aware scanning avoids interpreting literal \\$ or $$ display blocks
    as inline math. The contents of \\text{...} inside math are preserved.
    Malformed delimiters are kept instead of deleting the expression.
    """
    content, verbatim = _protect_verbatim(content)
    result: list[str] = []
    i = 0
    while i < len(content):
        math_env = _INLINE_MATH_ENV.match(content, i)
        if math_env and not _is_escaped(content, i):
            end_tag = _matching_environment_end(content, math_env)
            if end_tag is not None:
                body = _normalize_math_body(content[math_env.end():end_tag.start()])
                result.append("$" + body + "$")
                i = end_tag.end()
                continue
        if content.startswith(r"\(", i) and not _is_escaped(content, i):
            open_marker, close_marker = r"\(", r"\)"
        elif (content[i] == "$" and not _is_escaped(content, i)
              and not content.startswith("$$", i)
              and (i == 0 or content[i - 1] != "$")):
            open_marker = close_marker = "$"
        else:
            result.append(content[i])
            i += 1
            continue
        end = _next_unescaped(content, close_marker, i + len(open_marker))
        if end < 0:
            result.append(content[i])
            i += 1
            continue
        body = _normalize_math_body(content[i + len(open_marker):end])
        result.append("$" + body + "$")
        i = end + len(close_marker)
    return _restore_verbatim("".join(result), verbatim)


def _matching_environment_end(content: str, start: re.Match[str]) -> re.Match[str] | None:
    """Locate a matching end tag, including nested math environments.

    Return None for malformed nesting; the original source remains untouched.
    """
    name = _ENV_TAG.fullmatch(start.group()).group(2)
    stack = [name]
    for token in _ENV_TAG.finditer(content, start.end()):
        if _is_escaped(content, token.start()):
            continue
        kind, tag_name = token.groups()
        if kind == "begin":
            stack.append(tag_name)
        elif not stack or stack[-1] != tag_name:
            return None
        else:
            stack.pop()
            if not stack:
                return token
    return None


def _next_unescaped(content: str, marker: str, start: int) -> int:
    """Find the next delimiter that is not preceded by an odd count of '\\'."""
    at = content.find(marker, start)
    while at >= 0 and _is_escaped(content, at):
        at = content.find(marker, at + len(marker))
    return at


def _math_text_protect(content: str) -> tuple[str, dict[str, str]]:
    """Protect text-mode macro arguments: spaces inside them may matter."""
    stored: dict[str, str] = {}
    tag = "__M2MATH_TEXT_"
    while tag in content:
        tag = "_" + tag
    out: list[str] = []
    pos = 0
    for match in _MATH_TEXT_ARG.finditer(content):
        if match.start() < pos or _is_escaped(content, match.start()):
            continue
        opening = match.end()
        end = _matching_group_end(content, opening, "{", "}")
        if end is None:
            continue
        out.append(content[pos:match.start()])
        marker = f"{tag}{len(stored)}__"
        stored[marker] = content[match.start():end]
        out.append(marker)
        pos = end
    out.append(content[pos:])
    return "".join(out), stored


def _normalize_math_body(body: str) -> str:
    """Normalize insignificant layout whitespace without altering math tokens.

    In particular, TeX row separators (\\\\), alignment markers (&),
    labels, tags, and content in \\text{...} are left unchanged.
    """
    body, text_parts = _math_text_protect(body)
    body = re.sub(r"\s+", " ", body).strip()
    return _restore_verbatim(body, text_parts)


def normalize_block_math(content: str) -> str:
    r"""Canonicalize display math without changing multi-row semantics.

    $$...$$, \\[...\\], and displaymath are unnumbered, and become
    equation*. Existing equation/equation* preserve their number status.
    align, gather, multline, flalign, alignat, and eqnarray (including * forms)
    preserve their own environment so multiple numbering/labels remain valid.
    Whitespace outside text-mode math groups is collapsed deterministically.
    Unbalanced or unsupported environments are preserved intact.
    """
    content, verbatim = _protect_verbatim(content)
    out: list[str] = []
    pos = 0
    for match in _DISPLAY_START.finditer(content):
        if match.start() < pos or _is_escaped(content, match.start()):
            continue
        raw_open = match.group()
        if raw_open.startswith(r"\begin"):
            env = _ENV_TAG.fullmatch(raw_open).group(2)
            close = _matching_environment_end(content, match)
            if close is None:
                continue
            body = _normalize_math_body(content[match.end():close.start()])
            canonical_env = "equation*" if env == "displaymath" else env
            replacement = (
                rf"\begin{{{canonical_env}}}" + body + rf"\end{{{canonical_env}}}"
            )
            end = close.end()
        elif raw_open == "$$":
            close_at = _next_unescaped(content, "$$", match.end())
            if close_at < 0:
                continue
            body = _normalize_math_body(content[match.end():close_at])
            replacement = r"\begin{equation*}" + body + r"\end{equation*}"
            end = close_at + 2
        else:  # \\[
            close_at = _next_unescaped(content, r"\]", match.end())
            if close_at < 0:
                continue
            body = _normalize_math_body(content[match.end():close_at])
            replacement = r"\begin{equation*}" + body + r"\end{equation*}"
            end = close_at + 2
        out.append(content[pos:match.start()])
        out.append(replacement)
        pos = end
    out.append(content[pos:])
    return _restore_verbatim("".join(out), verbatim)


def _protect_math_spans(content: str) -> tuple[str, dict[str, str]]:
    """Keep math-mode whitespace intact during *text* whitespace cleanup."""
    tag = "__M2_MATH_"
    while tag in content:
        tag = "_" + tag
    stored: dict[str, str] = {}
    out: list[str] = []
    i = 0
    while i < len(content):
        finish = None
        match = _DISPLAY_START.match(content, i)
        if match and not _is_escaped(content, i):
            if match.group().startswith(r"\begin"):
                end_match = _matching_environment_end(content, match)
                finish = end_match.end() if end_match else None
            elif match.group() == "$$":
                at = _next_unescaped(content, "$$", match.end())
                finish = at + 2 if at >= 0 else None
            else:
                at = _next_unescaped(content, r"\]", match.end())
                finish = at + 2 if at >= 0 else None
        elif content.startswith(r"\(", i) and not _is_escaped(content, i):
            at = _next_unescaped(content, r"\)", i + 2)
            finish = at + 2 if at >= 0 else None
        elif _INLINE_MATH_ENV.match(content, i) and not _is_escaped(content, i):
            begin = _INLINE_MATH_ENV.match(content, i)
            end_tag = _matching_environment_end(content, begin)
            finish = end_tag.end() if end_tag else None
        elif content[i] == "$" and not _is_escaped(content, i):
            at = _next_unescaped(content, "$", i + 1)
            finish = at + 1 if at >= 0 else None
        if finish is not None:
            marker = f"{tag}{len(stored)}__"
            stored[marker] = content[i:finish]
            out.append(marker)
            i = finish
        else:
            out.append(content[i])
            i += 1
    return "".join(out), stored


def remove_formatting(content: str) -> str:
    """Unwrap known single-argument visual macros with balanced braces.

    Nested groups are supported. Unbalanced groups are kept unchanged.
    """
    while True:
        match = _FORMAT_COMMAND.search(content)
        if not match:
            return content
        opening = match.end()
        end = _matching_group_end(content, opening, "{", "}")
        if end is None:
            # Leave malformed input intact rather than silently deleting it.
            # Search later formatting macros, if any.
            next_pos = match.end()
            next_match = _FORMAT_COMMAND.search(content, next_pos)
            if next_match is None:
                return content
            # Keep the prefix through the malformed command; normalize the rest.
            return content[:next_match.start()] + remove_formatting(content[next_match.start():])
        content = content[:match.start()] + content[opening + 1:end - 1] + content[end:]


def normalize_whitespace(content: str) -> str:
    """Collapse whitespace outside semantic distinctions already parsed.

    Call this on node content (not on the full source document). In particular,
    preserve LaTeX source environments/line breaks during hierarchy parsing.
    """
    return re.sub(r"\s+", " ", content).strip()


def normalize_content(
    content: str,
    citation_mapping: Mapping[str, str] | None = None,
) -> str:
    """Normalize a semantic node's LaTeX content deterministically.

    Recommended order: hierarchy tokenization first; then this function for
    text/heading/figure/equation contents before TV4 exact deduplication.
    """
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    if citation_mapping is not None and not isinstance(citation_mapping, Mapping):
        raise TypeError("citation_mapping must be a mapping or None")
    content, verbatim = _protect_verbatim(content)
    content = strip_latex_comments(content)
    content = normalize_citations(content, citation_mapping)
    # Formatting macros inside math may be semantic (e.g. \texttt{...});
    # do not remove them as though they were ordinary paragraph markup.
    content, original_math = _protect_math_spans(content)
    content = remove_formatting(content)
    content = _LAYOUT.sub("", content)
    content = _restore_verbatim(content, original_math)
    content = normalize_inline_math(content)
    content = normalize_block_math(content)
    content, math_parts = _protect_math_spans(content)
    content = _restore_verbatim(normalize_whitespace(content), math_parts)
    return _restore_verbatim(content, verbatim)
