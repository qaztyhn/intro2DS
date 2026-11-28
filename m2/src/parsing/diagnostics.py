"""Output impact of per-version diagnostics from gathering, hierarchy and bibliography."""
import re

_IMPACT = {
    "BIBITEM_REPEATED_AUTHOR_UNRESOLVED": "affects_output",
    "BIBITEM_YEAR_UNRESOLVED": "affects_output",
    "BIB_SOURCE_CONFLICT": "affects_output",
    "BIB_DUPLICATE_FIELD_DIFFERENT": "affects_output",
    "BIB_DUPLICATE_FIELD_IDENTICAL": "info",
    "ENCODING_LATIN1_FALLBACK": "affects_output",
    "MISSING_CITATION_KEYS": "affects_output",
}

# Includes BibTeX inputs from which normalization derives an exported value.
_OUTPUT_FIELDS = {"title", "author", "authors", "year", "date", "doi", "journal",
                  "journaltitle", "url", "urlraw", "entry_type", "note", "howpublished",
                  "crossref", "arxiv_id", "citation_key"}
_CODE = re.compile(r"([A-Z][A-Z0-9_]+)(?=[:\s]|$)")


def duplicate_field_warning(key, name, kept, discarded):
    code = "BIB_DUPLICATE_FIELD_IDENTICAL" if kept == discarded else "BIB_DUPLICATE_FIELD_DIFFERENT"
    return f"{code} key={key} field={name}: duplicate field {name!r} ignored (first kept)"


def classify(message, *, missing_keys=False):
    """Return (code, impact). Only interpret codes at the start of a diagnostic."""
    context = re.match(r"^(?:\S+v\d+|\[\S+ v\d+\]) stage=(\w+)(?: |: )", message)
    payload = message[context.end():] if context else message
    if context and context[1] == "bib_parse" and payload.startswith("file="):
        payload = payload.partition(": ")[2]
    fallback = "BIBLIOGRAPHY_WARNINGS"
    if payload.startswith("RECOVERY_CONVERSION_WARNING: "):
        payload = payload.removeprefix("RECOVERY_CONVERSION_WARNING: ")
        payload = re.sub(r"^.*?\.bbl: ", "", payload, count=1, flags=re.I)
        fallback = "RECOVERY_CONVERSION_WARNING"
    match = _CODE.match(payload)
    code = match[1] if match else fallback
    if code == "AMSREFS_UNSUPPORTED_FIELD":
        field = re.fullmatch(r"AMSREFS_UNSUPPORTED_FIELD key=\S+ field=([\w-]+)", payload)
        if field and field[1].lower() not in _OUTPUT_FIELDS:
            return code, "info"
    if code == "MISSING_BIB_RESOURCE" and not missing_keys:
        return code, "info"
    return code, _IMPACT.get(code, "affects_output")


def diagnostic_codes(messages, *, missing_keys=False):
    reasons, info = set(), set()
    for message in messages:
        if re.match(r"^\S+v\d+ stage=root_selection reason=", message):
            continue  # User-provided provenance, not a diagnostic.
        code, impact = classify(message, missing_keys=missing_keys)
        (info if impact == "info" else reasons).add(code)
    return sorted(reasons), sorted(info)
