"""Offline tests for reference validation, pagination, and crawl results."""

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, call, patch

from src.stages import references
from src.utils.errors import (
    ClientError,
    InvalidResponseError,
    NetworkError,
    RateLimitError,
    RequestTimeoutError,
    ServerError,
)

ARXIV_ID = "1706.03762"


def make_reference(paper_id="paper-1", title="Example paper"):
    return {
        "citedPaper": {
            "paperId": paper_id,
            "title": title,
            "authors": [{"name": "Alice"}, {"name": "Bob"}],
            "externalIds": {"ArXiv": "1234.56789", "DOI": "10.example/paper"},
        }
    }


class ValidatePageTests(unittest.TestCase):
    def test_accepts_page_with_next_offset(self):
        data = [make_reference(), {"citedPaper": None}]
        self.assertEqual(
            references.validate_page({"offset": 10, "data": data, "next": 12}, 10),
            (data, 12),
        )

    def test_accepts_final_and_empty_pages(self):
        for data in ([], [make_reference()]):
            for ending in ({}, {"next": None}):
                with self.subTest(data=data, ending=ending):
                    page = {"offset": 0, "data": data, **ending}
                    self.assertEqual(references.validate_page(page, 0), (data, None))

    def test_rejects_non_object_responses(self):
        for response in (None, [], "invalid", 1, True):
            with self.subTest(response=response):
                with self.assertRaises(InvalidResponseError):
                    references.validate_page(response, 0)

    def test_rejects_missing_non_integer_and_unexpected_offsets(self):
        for offset in (
            {},
            {"offset": None},
            {"offset": True},
            {"offset": 0.0},
            {"offset": "0"},
            {"offset": -1},
            {"offset": 1},
        ):
            with self.subTest(offset=offset):
                with self.assertRaises(InvalidResponseError):
                    references.validate_page({"data": [], **offset}, 0)

    def test_rejects_missing_or_non_list_data(self):
        for data in ({}, {"data": None}, {"data": {}}, {"data": "invalid"}):
            with self.subTest(data=data):
                with self.assertRaises(InvalidResponseError):
                    references.validate_page({"offset": 0, **data}, 0)

    def test_rejects_malformed_reference_entries(self):
        for entry in (
            None,
            [],
            "invalid",
            {"citedPaper": []},
            {"citedPaper": "invalid"},
            {"citedPaper": 1},
        ):
            with self.subTest(entry=entry):
                with self.assertRaises(InvalidResponseError):
                    references.validate_page({"offset": 0, "data": [entry]}, 0)

    def test_accepts_missing_or_unresolved_cited_papers(self):
        data = [{}, {"citedPaper": None}, {"citedPaper": {}}]
        self.assertEqual(
            references.validate_page({"offset": 0, "data": data}, 0),
            (data, None),
        )

    def test_rejects_invalid_next_offsets(self):
        for next_offset in (True, 11.0, "11", -1, 0, 10):
            with self.subTest(next_offset=next_offset):
                with self.assertRaises(InvalidResponseError):
                    references.validate_page(
                        {"offset": 10, "data": [make_reference()], "next": next_offset},
                        10,
                    )

    def test_rejects_empty_page_that_claims_more_results(self):
        with self.assertRaises(InvalidResponseError):
            references.validate_page({"offset": 0, "data": [], "next": 1}, 0)


class FetchReferencesTests(unittest.TestCase):
    def setUp(self):
        env_patch = patch.dict(references.os.environ, {}, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        request_patch = patch.object(references, "request_json", autospec=True)
        self.request_json = request_patch.start()
        self.addCleanup(request_patch.stop)

    def test_collects_pages_using_server_next_offset(self):
        first, second = make_reference(), make_reference("paper-2", "Second paper")
        self.request_json.side_effect = [
            {"offset": 0, "data": [first], "next": 7},
            {"offset": 7, "data": [second]},
        ]
        hook = Mock()

        result = references.fetch_references(
            ARXIV_ID, timeout=10, max_retries=2, on_attempt=hook
        )

        self.assertEqual(result, [first, second])
        url = f"https://api.semanticscholar.org/graph/v1/paper/ARXIV:{ARXIV_ID}/references"
        self.assertEqual(
            self.request_json.call_args_list,
            [
                call(
                    url,
                    params={
                        "fields": "title,authors,externalIds",
                        "offset": offset,
                        "limit": 1000,
                    },
                    headers={},
                    throttle=references.S2,
                    timeout=10,
                    max_retries=2,
                    on_attempt=hook,
                )
                for offset in (0, 7)
            ],
        )

    def test_sends_api_key_from_environment(self):
        self.request_json.return_value = {"offset": 0, "data": []}
        with patch.dict(references.os.environ, {"S2_API_KEY": "test-key"}):
            references.fetch_references(ARXIV_ID)
        self.assertEqual(
            self.request_json.call_args.kwargs["headers"], {"x-api-key": "test-key"}
        )

    def test_empty_first_page_stops_pagination(self):
        self.request_json.return_value = {"offset": 0, "data": []}
        self.assertEqual(references.fetch_references(ARXIV_ID), [])
        self.request_json.assert_called_once()

    def test_invalid_later_page_does_not_return_partial_results(self):
        self.request_json.side_effect = [
            {"offset": 0, "data": [make_reference()], "next": 1},
            {"offset": 0, "data": [make_reference()]},
        ]
        with self.assertRaises(InvalidResponseError):
            references.fetch_references(ARXIV_ID)
        self.assertEqual(self.request_json.call_count, 2)

    def test_request_failure_propagates(self):
        error = NetworkError()
        self.request_json.side_effect = error
        with self.assertRaises(NetworkError) as caught:
            references.fetch_references(ARXIV_ID)
        self.assertIs(caught.exception, error)
        self.request_json.assert_called_once()


class NormalizeReferenceTests(unittest.TestCase):
    def test_maps_paper_fields(self):
        self.assertEqual(
            references.normalize_reference(ARXIV_ID, make_reference()),
            {
                "arxiv_id": ARXIV_ID,
                "ref_arxiv_id": "1234.56789",
                "ref_title": "Example paper",
                "ref_authors": ["Alice", "Bob"],
                "ref_submission_date": None,
                "ref_s2_id": "paper-1",
            },
        )

    def test_skips_unresolved_cited_papers(self):
        for reference in ({}, {"citedPaper": None}):
            with self.subTest(reference=reference):
                self.assertIsNone(references.normalize_reference(ARXIV_ID, reference))

    def test_missing_optional_fields_have_empty_defaults(self):
        self.assertEqual(
            references.normalize_reference(ARXIV_ID, {"citedPaper": {}}),
            {
                "arxiv_id": ARXIV_ID,
                "ref_arxiv_id": None,
                "ref_title": None,
                "ref_authors": [],
                "ref_submission_date": None,
                "ref_s2_id": None,
            },
        )

    def test_ignores_invalid_author_entries(self):
        reference = {
            "citedPaper": {
                "authors": [
                    None,
                    "invalid",
                    {},
                    {"name": None},
                    {"name": 42},
                    {"name": "Alice"},
                    {"name": "Bob"},
                ]
            }
        }
        result = references.normalize_reference(ARXIV_ID, reference)
        self.assertEqual(result["ref_authors"], ["Alice", "Bob"])

    def test_tolerates_non_list_authors_and_non_object_external_ids(self):
        for authors in (None, "Alice", {}, 42):
            for external_ids in (None, [], "invalid", 42):
                with self.subTest(authors=authors, external_ids=external_ids):
                    result = references.normalize_reference(
                        ARXIV_ID,
                        {
                            "citedPaper": {
                                "authors": authors,
                                "externalIds": external_ids,
                            }
                        },
                    )
                    self.assertEqual(result["ref_authors"], [])
                    self.assertIsNone(result["ref_arxiv_id"])

    def test_normalizes_list_in_order_and_skips_unresolved_papers(self):
        result = references.normalize_references(
            ARXIV_ID,
            [
                {"citedPaper": None},
                make_reference(),
                {},
                make_reference("paper-2"),
            ],
        )
        self.assertEqual([item["ref_s2_id"] for item in result], ["paper-1", "paper-2"])
        self.assertEqual(references.normalize_references(ARXIV_ID, []), [])


class CrawlReferencesTests(unittest.TestCase):
    def setUp(self):
        request_patch = patch.object(references, "request_json", autospec=True)
        self.request_json = request_patch.start()
        self.addCleanup(request_patch.stop)

    def assert_result_metadata(self, result):
        self.assertEqual(
            set(result),
            {"arxiv_id", "status", "error_code", "source", "fetched_at", "references"},
        )
        self.assertEqual(result["arxiv_id"], ARXIV_ID)
        self.assertEqual(result["source"], "semantic_scholar")
        fetched_at = datetime.fromisoformat(result["fetched_at"])
        self.assertEqual(fetched_at.utcoffset(), timezone.utc.utcoffset(fetched_at))

    def test_success_fetches_and_normalizes_all_pages(self):
        self.request_json.side_effect = [
            {"offset": 0, "data": [make_reference(), {"citedPaper": None}], "next": 2},
            {"offset": 2, "data": [make_reference("paper-2")]},
        ]
        started = datetime.now(timezone.utc)
        result = references.crawl_references(ARXIV_ID, max_retries=0)

        self.assert_result_metadata(result)
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["error_code"])
        self.assertEqual(
            [item["ref_s2_id"] for item in result["references"]], ["paper-1", "paper-2"]
        )
        self.assertGreaterEqual(datetime.fromisoformat(result["fetched_at"]), started)
        self.assertLessEqual(
            datetime.fromisoformat(result["fetched_at"]), datetime.now(timezone.utc)
        )
        self.assertEqual(self.request_json.call_count, 2)
        self.assertEqual(self.request_json.call_args.kwargs["max_retries"], 0)

    def test_empty_response_reports_no_references(self):
        self.request_json.return_value = {"offset": 0, "data": []}
        result = references.crawl_references(ARXIV_ID)
        self.assert_result_metadata(result)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["error_code"], "NO_REFERENCES_RETURNED")
        self.assertEqual(result["references"], [])

    def test_crawl_errors_become_failed_results(self):
        for error, expected_code in (
            (NetworkError(), "NETWORK_ERROR"),
            (RequestTimeoutError(), "TIMEOUT"),
            (RateLimitError(), "HTTP_429"),
            (ClientError(404), "NOT_FOUND"),
            (ClientError(403), "HTTP_4XX"),
            (ServerError(503), "HTTP_5XX"),
            (InvalidResponseError(), "INVALID_RESPONSE"),
        ):
            with self.subTest(error=type(error).__name__, code=expected_code):
                self.request_json.side_effect = error
                result = references.crawl_references(ARXIV_ID)
                self.assert_result_metadata(result)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error_code"], expected_code)
                self.assertEqual(result["references"], [])

    def test_malformed_response_reports_failed_validation(self):
        self.request_json.return_value = {"offset": 0, "data": "invalid"}
        result = references.crawl_references(ARXIV_ID)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "INVALID_RESPONSE")
        self.assertEqual(result["references"], [])

    def test_later_page_failure_discards_partial_references(self):
        self.request_json.side_effect = [
            {"offset": 0, "data": [make_reference()], "next": 1},
            NetworkError(),
        ]
        result = references.crawl_references(ARXIV_ID)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "NETWORK_ERROR")
        self.assertEqual(result["references"], [])
        self.assertEqual(self.request_json.call_count, 2)


if __name__ == "__main__":
    unittest.main()
