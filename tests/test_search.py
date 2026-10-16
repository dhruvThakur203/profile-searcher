from src.tools.search import FakeSearchTool, SearchResult


def test_registered_query_returns_results_in_order():
    tool = FakeSearchTool()
    first = SearchResult(
        title="First",
        url="https://example.com/first",
        snippet="First snippet",
    )
    second = SearchResult(
        title="Second",
        url="https://example.com/second",
        snippet="Second snippet",
    )
    tool.register(query="example query", results=[first, second])

    results = tool.search("example query")

    assert results == [first, second]


def test_unknown_query_returns_empty_list():
    tool = FakeSearchTool()

    assert tool.search("unregistered query") == []


def test_result_structure_has_title_url_and_snippet():
    tool = FakeSearchTool()
    tool.register(
        query="example query",
        results=[
            SearchResult(
                title="Example",
                url="https://example.com",
                snippet="Example snippet",
            )
        ],
    )

    result = tool.search("example query")[0]

    assert isinstance(result, SearchResult)
    assert result.title == "Example"
    assert result.url == "https://example.com"
    assert result.snippet == "Example snippet"


def test_exact_query_isolation():
    tool = FakeSearchTool()
    tool.register(
        query="Dhruv Singh Thakur Singapore",
        results=[
            SearchResult(
                title="Exact name",
                url="https://example.com/exact",
                snippet="Registered only for the full name",
            )
        ],
    )

    assert tool.search("Dhruv Thakur Singapore") == []
