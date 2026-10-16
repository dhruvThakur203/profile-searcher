import json

from src.scraper.search_client import (
    IDENTIFYING_USER_AGENT,
    BraveSearchClient,
    SearchStatus,
)


def _client(transport, api_key="test-key"):
    return BraveSearchClient(api_key=api_key, transport=transport, timeout=5)


def test_brave_client_parses_title_url_and_snippet_in_order():
    payload = {
        "web": {
            "results": [
                {
                    "title": "One",
                    "url": "https://one.example/a",
                    "description": "First snippet",
                },
                {
                    "title": "Two",
                    "url": "https://two.example/b",
                    "description": "Second snippet",
                    "extra_snippets": ["More public text"],
                },
            ]
        }
    }
    seen = {}

    def transport(url, headers, timeout):
        seen["url"] = url
        seen["headers"] = headers
        seen["timeout"] = timeout
        return 200, json.dumps(payload).encode()

    response = _client(transport).search("Dhruv Singh Thakur")

    assert response.status == SearchStatus.OK
    assert [(item.title, item.url, item.snippet) for item in response.results] == [
        ("One", "https://one.example/a", "First snippet"),
        ("Two", "https://two.example/b", "Second snippet\nMore public text"),
    ]
    assert "Dhruv" in seen["url"]
    assert "Singh" in seen["url"]
    assert "Thakur" in seen["url"]
    assert "count=10" in seen["url"]
    assert seen["headers"]["Accept"] == "application/json"
    assert seen["headers"]["User-Agent"] == IDENTIFYING_USER_AGENT
    assert "Mozilla" not in seen["headers"]["User-Agent"]
    assert seen["headers"]["X-Subscription-Token"] == "test-key"
    assert seen["timeout"] == 5


def test_brave_client_reports_access_denial_as_blocked():
    def transport(url, headers, timeout):
        return 403, b"access denied"

    response = _client(transport).search("Dhruv Singh Thakur")

    assert response.status == SearchStatus.BLOCKED
    assert response.retryable is False
    assert response.results == []


def test_brave_client_reports_html_block_page_without_parsing_it():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        return 200, b"<html><body>captcha required</body></html>"

    response = _client(transport).search("Dhruv Singh Thakur")

    assert response.status == SearchStatus.BLOCKED
    assert response.results == []
    assert len(calls) == 1


def test_brave_client_does_not_call_network_without_an_api_key():
    def transport(url, headers, timeout):
        raise AssertionError("transport should not be called")

    response = _client(transport, api_key="  ").search("Dhruv Singh Thakur")

    assert response.status == SearchStatus.ERROR
    assert response.results == []
    assert "No request was sent" in response.message


def test_brave_client_marks_rate_limit_as_retryable_error():
    def transport(url, headers, timeout):
        return 429, b"slow down"

    response = _client(transport).search("Dhruv Singh Thakur")

    assert response.status == SearchStatus.ERROR
    assert response.retryable is True
