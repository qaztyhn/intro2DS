"""M2 TV2 hierarchy tests; GatheredDocument mimics TV1's output."""

from __future__ import annotations

import pytest

from src.parsing.contracts import GatheredDocument, ParsedNode
from src.parsing.hierarchy import parse_hierarchy


def gathered(latex: str, status: str = "ok") -> GatheredDocument:
    return GatheredDocument(
        "2507.11662",
        2,
        "main.tex",
        latex,
        ["main.tex"],
        {},
        status
    )


def nodes(node: ParsedNode | None, kind: str) -> list[ParsedNode]:
    if node is None:
        return []

    return ([node] if node.kind == kind else []) + [
        descendant
        for child in node.children
        for descendant in nodes(child, kind)
    ]


def test_document_sections_nested_parent_child_order():
    text = r"""\begin{document}
\section{Introduction}
First sentence. Second sentence.
\subsection{Motivation}
Method works.
\section{Results}
Results hold.
\end{document}"""

    doc = parse_hierarchy(gathered(text), {})

    assert doc.status == "ok"
    assert doc.root.kind == "document"
    assert [n.kind for n in doc.root.children] == [
        "section", "section"
    ]

    introduction, results = doc.root.children

    assert introduction.content == "Introduction"
    assert results.content == "Results"

    assert [n.kind for n in introduction.children] == [
        "paragraph", "subsection"
    ]

    assert [
        n.content
        for n in nodes(introduction.children[0], "sentence")
    ] == [
        "First sentence.",
        "Second sentence."
    ]

    assert introduction.children[1].content == "Motivation"

    assert [
        s.content for s in nodes(results, "sentence")
    ] == ["Results hold."]


def test_paragraphs_split_on_blank_lines_and_par():
    text = (
        r"\begin{document}"
        + "A. B.\n\nC. D."
        + r"\par E. F."
        + r"\end{document}"
    )

    doc = parse_hierarchy(gathered(text), {})

    assert len(nodes(doc.root, "paragraph")) == 3
    assert len(nodes(doc.root, "sentence")) == 6


def test_empty_last_sentence_without_period_is_not_lost():
    doc = parse_hierarchy(
        gathered(
            r"\begin{document}"
            r"A final fragment"
            r"\end{document}"
        ),
        {}
    )

    assert [
        x.content for x in nodes(doc.root, "sentence")
    ] == ["A final fragment"]


def test_math_inside_sentence_not_split_at_decimal_or_citation_dot():
    text = (
        r"\begin{document}"
        r"Value $x = 3.14$ holds. "
        r"See \cite{smith.2020} too."
        r"\end{document}"
    )

    doc = parse_hierarchy(gathered(text), {})

    assert [
        s.content for s in nodes(doc.root, "sentence")
    ] == [
        "Value $x = 3.14$ holds.",
        r"See \cite{smith.2020} too."
    ]


def test_abbreviations_avoid_false_split():
    text = (
        r"\begin{document}"
        r"Fig. 1 shows results. E.g. a test."
        r"\end{document}"
    )

    doc = parse_hierarchy(gathered(text), {})

    assert len(nodes(doc.root, "sentence")) == 2


def test_citations_and_formatting_in_sentence():
    text = (
        r"\begin{document}"
        r"\section{Intro}"
        r"We use \textbf{Deep Learning} \cite{old}."
        r"\end{document}"
    )

    doc = parse_hierarchy(
        gathered(text),
        {"old": "canonical"}
    )

    assert [
        n.content for n in nodes(doc.root, "sentence")
    ] == [
        r"We use Deep Learning \cite{canonical}."
    ]


@pytest.mark.parametrize(
    "math",
    [
        r"\[ x+2 \]",
        r"$$ x+2 $$",
        r"\begin{align} a &= b \\ c &= d \end{align}",
        r"\begin{gather*} x \\ y \end{gather*}"
    ]
)
def test_block_math_becomes_equation_leaf(math):
    text = (
        r"\begin{document}Before. "
        + math
        + r" After.\end{document}"
    )

    doc = parse_hierarchy(gathered(text), {})

    assert doc.status == "ok"

    equations = nodes(doc.root, "equation")

    assert len(equations) == 1
    assert equations[0].children == []

    assert [
        s.content for s in nodes(doc.root, "sentence")
    ] == ["Before.", "After."]

    assert (
        "x+2" in equations[0].content
        or "&=" in equations[0].content
        or r"\\" in equations[0].content
    )


def test_numbered_equation_label_retained():
    text = (
        r"\begin{document}"
        r"\begin{equation}"
        r"x=2\label{eq:x}"
        r"\end{equation}"
        r"\end{document}"
    )

    doc = parse_hierarchy(gathered(text), {})

    assert nodes(doc.root, "equation")[0].content == (
        r"\begin{equation}"
        r"x=2\label{eq:x}"
        r"\end{equation}"
    )


@pytest.mark.parametrize(
    "name",
    ["figure", "figure*", "table", "table*", "longtable"]
)
def test_figure_and_table_are_figure_leaves(name):
    source = (
        rf"\begin{{document}}\begin{{{name}}}[htbp]"
        r"\centering \caption{Our result} \label{tab:a} "
        r"\begin{tabular}{cc}A & B\\ 1 & 2\end{tabular}"
        + rf"\end{{{name}}}\end{{document}}"
    )

    doc = parse_hierarchy(gathered(source), {})
    figures = nodes(doc.root, "figure")

    assert len(figures) == 1
    assert figures[0].children == []
    assert "Our result" in figures[0].content
    assert r"\label{tab:a}" in figures[0].content

    if name != "longtable":
        assert not figures[0].content.startswith("[htbp]")


def test_nested_lists_items_and_order():
    latex = r"""\begin{document}
\begin{itemize}
\item First item.
\item Second item.
    \begin{enumerate}
    \item Nested one.
    \item Nested two.
    \end{enumerate}
\item Third item.
\end{itemize}
\end{document}"""

    result = parse_hierarchy(gathered(latex), {})

    outer = result.root.children[0]

    assert result.status == "ok"
    assert outer.kind == "list"

    assert [n.kind for n in outer.children] == [
        "list_item"
    ] * 3

    assert [
        x.content for x in nodes(outer, "sentence")
    ] == [
        "First item.",
        "Second item.",
        "Nested one.",
        "Nested two.",
        "Third item."
    ]

    assert [
        x.kind for x in outer.children[1].children
    ] == [
        "paragraph",
        "list"
    ]

    assert "Nested two." in outer.content

    assert len({
        x.local_id
        for x in nodes(result.root, "list_item")
    }) == 5


def test_description_item_optional_label():
    result = parse_hierarchy(
        gathered(
            r"\begin{document}"
            r"\begin{description}"
            r"\item[Idea] A concept."
            r"\end{description}"
            r"\end{document}"
        ),
        {}
    )

    item = nodes(result.root, "list_item")[0]

    assert "Idea" in item.content
    assert "A concept." in item.content


def test_abstract_and_acknowledgements_and_appendices_included():
    latex = r"""\begin{document}
\begin{abstract}An abstract.\end{abstract}
\section{Introduction}Main text.
\section*{Acknowledgements}Thanks to everyone.
\appendix
\section{Proofs}Proof body.
\end{document}"""

    doc = parse_hierarchy(gathered(latex), {})

    assert doc.status == "ok"
    assert len(nodes(doc.root, "abstract")) == 1

    assert len(
        nodes(doc.root, "acknowledgements")
    ) == 1

    appendix = nodes(doc.root, "appendix")[0]

    assert [
        n.kind for n in appendix.children
    ] == ["section"]

    assert appendix.children[0].content == "Proofs"

    assert "Proof body." in [
        n.content
        for n in nodes(appendix, "sentence")
    ]


def test_starred_appendix_heading_included():
    doc = parse_hierarchy(
        gathered(
            r"\begin{document}"
            r"\section*{Appendix A}"
            r"Supplementary proof."
            r"\end{document}"
        ),
        {}
    )

    assert nodes(
        doc.root,
        "appendix"
    )[0].content == "Appendix A"


def test_bibliography_heading_excluded_but_next_appendix_preserved():
    latex = r"""\begin{document}
\section{Method}Main text.
\section*{References}
Doe 2020. Smith 2021.
\section*{Appendix A}
Appendix content.
\end{document}"""

    doc = parse_hierarchy(gathered(latex), {})

    all_content = " ".join(
        n.content
        for n in nodes(doc.root, "sentence")
    )

    assert "Doe" not in all_content
    assert "Smith" not in all_content
    assert "Appendix content." in all_content
    assert len(nodes(doc.root, "appendix")) == 1


def test_thebibliography_and_bibliography_command_excluded():
    latex = r"""\begin{document}
Text before.
\begin{thebibliography}{9}
\bibitem{key} Reference title.
\end{thebibliography}
\bibliography{refs}
Text after.
\end{document}"""

    doc = parse_hierarchy(gathered(latex), {})

    all_content = " ".join(
        n.content
        for n in nodes(doc.root, "sentence")
    )

    assert "Reference title" not in all_content
    assert "Text before." in all_content
    assert "Text after." in all_content


def test_preamble_not_parsed_and_title_used_as_root():
    latex = r"""\documentclass{article}
\title{\textbf{My Paper}}
\newcommand{\mymacro}{SECRET PREAMBLE}
\begin{document}\maketitle Main text.\end{document}"""

    doc = parse_hierarchy(gathered(latex), {})

    assert doc.root.content == "My Paper"

    contents = " ".join(
        n.content
        for n in nodes(doc.root, "sentence")
    )

    assert "SECRET PREAMBLE" not in contents
    assert "Main text." in contents


def test_comment_sections_ignored():
    latex = (
        r"\begin{document}"
        + "\n% \\section{Fake}\n"
        + r"\section{Real}Real text.\end{document}"
    )

    doc = parse_hierarchy(gathered(latex), {})

    assert [
        n.content for n in nodes(doc.root, "section")
    ] == ["Real"]


def test_missing_environment_returns_partial_with_warning_not_crash():
    doc = parse_hierarchy(
        gathered(
            r"\section{Introduction}Works."
        ),
        {}
    )

    assert doc.status == "partial"
    assert "DOCUMENT_ENV_MISSING" in doc.warnings

    assert "Works." in [
        n.content
        for n in nodes(doc.root, "sentence")
    ]


def test_unclosed_equation_marks_partial_without_losing_text():
    doc = parse_hierarchy(
        gathered(
            r"\begin{document}"
            r"A. "
            r"\begin{equation}x = 1"
            r"\end{document}"
        ),
        {}
    )

    assert doc.status == "partial"
    assert "UNCLOSED_ENV:equation" in doc.warnings


@pytest.mark.parametrize(
    "status",
    ["no_tex", "failed", "skipped"]
)
def test_unavailable_source_propagates_status_and_no_root(status):
    doc = parse_hierarchy(
        gathered("", status),
        {}
    )

    assert doc.root is None
    assert doc.status == status


def test_empty_gathered_tex_no_tex():
    doc = parse_hierarchy(
        gathered("  "),
        {}
    )

    assert doc.root is None
    assert doc.status == "no_tex"
    assert "EMPTY_LATEX" in doc.warnings


def test_partial_gathered_status_is_preserved():
    doc = parse_hierarchy(
        gathered(
            r"\begin{document}A.\end{document}",
            "partial"
        ),
        {}
    )

    assert doc.status == "partial"
    assert doc.root is not None


def test_deterministic_local_ids_per_version():
    raw = (
        r"\begin{document}"
        r"\section{Intro}"
        r"A. B."
        r"\end{document}"
    )

    a = parse_hierarchy(gathered(raw), {})
    b = parse_hierarchy(gathered(raw), {})

    assert [
        n.local_id
        for n in nodes(a.root, "sentence")
    ] == [
        n.local_id
        for n in nodes(b.root, "sentence")
    ]

    assert a.root.local_id == "n000000"


def test_heading_levels_part_chapter_section_paragraph_heading():
    latex = r"""\begin{document}
\part{Part I}
\chapter{Chapter 1}
\section{Section 1}
\paragraph{Small heading}Text.
\end{document}"""

    doc = parse_hierarchy(gathered(latex), {})

    part = doc.root.children[0]

    assert part.kind == "part"
    assert part.children[0].kind == "chapter"
    assert part.children[0].children[0].kind == "section"

    heading = part.children[0].children[0].children[0]

    assert heading.kind == "paragraph_heading"

    assert [
        s.content
        for s in nodes(heading, "sentence")
    ] == ["Text."]


def test_unknown_wrapper_preserves_prose():
    latex = (
        r"\begin{document}"
        r"\begin{quote}Quoted content.\end{quote}"
        r"\end{document}"
    )

    doc = parse_hierarchy(gathered(latex), {})

    assert [
        s.content
        for s in nodes(doc.root, "sentence")
    ] == ["Quoted content."]


def test_footnote_contains_fake_section_without_creating_heading():
    source = (
        r"\begin{document}"
        r"\section{Real}"
        r"A sentence "
        r"\footnote{"
        r"The word \section{Fake} is shown as an example"
        r"}."
        r"\end{document}"
    )

    doc = parse_hierarchy(gathered(source), {})

    assert [
        s.content
        for s in nodes(doc.root, "section")
    ] == ["Real"]


def test_bibliography_heading_does_not_swallow_later_section():
    source = (
        r"\begin{document}"
        r"\section{Intro}A."
        r"\section{Bibliography}References."
        r"\section{Notes}This is preserved."
        r"\end{document}"
    )

    doc = parse_hierarchy(gathered(source), {})

    assert [
        n.content
        for n in nodes(doc.root, "section")
    ] == [
        "Intro",
        "Notes"
    ]

    assert "References." not in [
        s.content
        for s in nodes(doc.root, "sentence")
    ]


def test_list_optional_layout_not_a_paragraph():
    source = (
        r"\begin{document}"
        r"\begin{enumerate}[label=(\alph*)]"
        r"\item First."
        r"\item Second."
        r"\end{enumerate}"
        r"\end{document}"
    )

    result = parse_hierarchy(
        gathered(source),
        {}
    )

    lst = nodes(result.root, "list")[0]

    assert [
        n.kind for n in lst.children
    ] == [
        "list_item",
        "list_item"
    ]

    assert result.status == "ok"


def test_unknown_macro_with_math_inside_remains_in_text():
    source = (
        r"\begin{document}"
        r"We use \newterm{data} in "
        r"$x=1$."
        r"\end{document}"
    )

    result = parse_hierarchy(
        gathered(source),
        {}
    )

    sentences = nodes(result.root, "sentence")

    assert len(sentences) == 1
    assert r"\newterm{data}" in sentences[0].content


def test_unclosed_document_end_is_partial():
    result = parse_hierarchy(
        gathered(
            r"\begin{document}A sentence."
        ),
        {}
    )

    assert result.status == "partial"
    assert "DOCUMENT_END_MISSING" in result.warnings

    assert "A sentence." in [
        s.content
        for s in nodes(result.root, "sentence")
    ]


def test_unresolved_include_is_preserved_but_marks_partial():
    source = (
        r"\begin{document}"
        r"\section{Intro}"
        r"\input{sections/missing}"
        r"\end{document}"
    )

    result = parse_hierarchy(
        gathered(source),
        {}
    )

    assert result.status == "partial"
    assert "UNRESOLVED_INCLUDE" in result.warnings

    assert r"\input{sections/missing}" in [
        n.content
        for n in nodes(result.root, "sentence")
    ]