"""Fetch and normalize outgoing paper references from Semantic Scholar."""

import os
from datetime import datetime, timezone
from ..utils.errors import InvalidResponseError
from ..utils.http import request_json, S2
from ..utils.errors import (
    CrawlError,
    InvalidResponseError
)

BASE_URL = "https://api.semanticscholar.org/graph/v1/paper"   
LIMIT = 1000

def validate_page(response, expected_offset):
    """Validate a response page and return its references and next offset.

    Raise InvalidResponseError for malformed data or inconsistent pagination.
    A missing or null next offset marks the final page.
    """
    if not isinstance(response, dict):
        raise InvalidResponseError()

    offset = response.get("offset")
    # Reject booleans too: bool is a subclass of int in Python.
    if type(offset) is not int:
        raise InvalidResponseError()

    # Do not silently accept a repeated or unexpected page.
    if offset != expected_offset:
        raise InvalidResponseError()

    references = response.get("data")

    if not isinstance(references, list):
        raise InvalidResponseError()

    for reference in references:
        if not isinstance(reference, dict):
            raise InvalidResponseError()

        # Unresolved cited papers may be absent or null; normalization skips them.
        cited_paper = reference.get("citedPaper")

        if cited_paper is not None and not isinstance(cited_paper, dict):
            raise InvalidResponseError()

    next_offset = response.get("next")
    if next_offset is not None:
        if type(next_offset) is not int:
            raise InvalidResponseError()

        # Pagination must move forward to prevent an endless fetch loop.
        if next_offset <= offset:
            raise InvalidResponseError()

        # An empty page with a continuation offset is treated as inconsistent.
        if len(references) == 0:
            raise InvalidResponseError()

    return references, next_offset



def fetch_references(arxiv_id, **kwargs):
    """Collect all reference pages for an arXiv paper.

    Forward request options such as timeout, max_retries, and on_attempt to
    request_json. Request and validation errors propagate to the caller.
    """
    url = f"{BASE_URL}/ARXIV:{arxiv_id}/references"

    # Copy caller headers; an environment API key takes precedence if present.
    headers = dict(kwargs.pop("headers", None) or {})
    api_key = os.getenv("S2_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key

    all_references = []
    offset = 0

    while True:
        params = {
            "fields": "title,authors,externalIds",
            "offset": offset,
            "limit": LIMIT
        }

        response = request_json(url, 
                                params=params, 
                                headers=headers, 
                                throttle=S2, 
                                **kwargs)

        references, next_offset = validate_page(response, offset)

        all_references.extend(references)

        if next_offset is None:
            break

        # Follow the API's next offset rather than deriving it from page length.
        offset = next_offset
        
    return all_references


def normalize_reference(arxiv_id, reference):
    """Map a validated reference to the output schema, or None if unresolved.

    arxiv_id identifies the source paper; ref_* fields describe the cited paper.
    Missing optional metadata is represented by None or an empty author list.
    """
    paper = reference.get("citedPaper")

    if paper is None:
        return None

    raw_authors = paper.get("authors")

    # Optional metadata can be missing or malformed even on a valid page.
    if not isinstance(raw_authors, list):
        raw_authors = []
    
    external_ids = paper.get("externalIds")

    if not isinstance(external_ids, dict):
        external_ids = {}

    ref_arxiv_id = external_ids.get("ArXiv")
    
    authors = []

    for raw_author in raw_authors:
        if not isinstance(raw_author, dict):
            continue

        name = raw_author.get("name")
        if isinstance(name, str):
            authors.append(name)

    result = {
        "arxiv_id": arxiv_id,
        "ref_arxiv_id": ref_arxiv_id,
        "ref_title": paper.get("title"),
        "ref_authors": authors,
        # The requested API fields do not include a submission date.
        "ref_submission_date": None,
        "ref_s2_id": paper.get("paperId")
    }

    return result


def normalize_references(arxiv_id, references):
    """Normalize references in order, omitting unresolved cited papers."""
    results = []
    for reference in references:
        normalized = normalize_reference(arxiv_id,reference)
        if normalized is not None:
            results.append(normalized)

    return results



def crawl_references(arxiv_id, **kwargs):
    """Return references with crawl status, error code, and a UTC timestamp.

    Status is ok for nonempty raw results, empty when no references are returned,
    or failed when a CrawlError prevents the crawl from completing.
    """
    try:
        raw_references = fetch_references(arxiv_id, **kwargs)

        normalized_references = normalize_references(arxiv_id, raw_references)

        # Status reflects raw results, even if normalization skips every entry.
        if len(raw_references) == 0:
            status = "empty"
            error_code = "NO_REFERENCES_RETURNED"
        else:
            status = "ok"
            error_code = None
    except CrawlError as e:
        # Discard partial results if any page fails so failed crawls stay empty.
        raw_references = []
        normalized_references = []
        error_code = e.error_code
        status = "failed"

    return {
        "arxiv_id": arxiv_id,
        "status": status,
        "error_code": error_code,
        "source": "semantic_scholar",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "references": normalized_references
    }

