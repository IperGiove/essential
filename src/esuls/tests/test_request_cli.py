"""
Tests for request_cli HTTP utilities.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

from esuls.request_cli import (
    Response,
    make_request,
    make_request_cffi,
    close_shared_client,
    _get_user_agent,
    _extract_domain,
    _apply_jitter,
    _get_domain_client,
    _req_loop_state,
)


async def test_response_object():
    """Test Response dataclass creation and accessors."""
    resp = Response(
        status_code=200,
        headers={"content-type": "application/json"},
        _content=b'{"key": "value"}',
        text='{"key": "value"}',
        url="https://example.com",
    )
    assert resp.status_code == 200
    assert resp.content == b'{"key": "value"}'
    assert resp.json() == {"key": "value"}
    assert resp.url == "https://example.com"
    assert resp.headers["content-type"] == "application/json"
    print("  [PASS] Response object creation and accessors")


async def test_response_json_error():
    """Test Response.json() raises on invalid JSON."""
    resp = Response(
        status_code=200,
        headers={},
        _content=b"not json",
        text="not json",
    )
    try:
        resp.json()
        assert False, "Should have raised JSONDecodeError"
    except json.JSONDecodeError:
        pass
    print("  [PASS] Response.json() raises on invalid JSON")


async def test_extract_domain():
    """Test domain extraction from URLs."""
    assert _extract_domain("https://example.com/path") == "https://example.com"
    assert _extract_domain("http://api.test.io:8080/v1") == "http://api.test.io:8080"
    assert _extract_domain("https://a.b.c/x?q=1") == "https://a.b.c"
    print("  [PASS] Domain extraction")


async def test_apply_jitter():
    """Test jitter calculation."""
    assert _apply_jitter(10.0, 0) == 10.0
    assert _apply_jitter(10.0, -1) == 10.0

    result = _apply_jitter(10.0, 0.5)
    assert 10.0 <= result <= 15.0
    print("  [PASS] Jitter calculation")


async def test_get_user_agent_fallback():
    """Test that _get_user_agent returns fallback on failure."""
    with patch("esuls.request_cli._user_agent_cache", {"instance": None}):
        with patch(
            "esuls.request_cli.UserAgent",
            side_effect=OSError("test"),
        ):
            agent = await _get_user_agent()
            assert "Mozilla" in agent
    print("  [PASS] UserAgent fallback on error")


async def test_make_request_success():
    """Test make_request with a mocked successful response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/html"}
    mock_response.content = b"<html>OK</html>"
    mock_response.text = "<html>OK</html>"
    mock_response.url = "https://example.com"

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.is_closed = False

    with patch(
        "esuls.request_cli._get_domain_client",
        return_value=mock_client,
    ):
        resp = await make_request("https://example.com", method="GET")

    assert resp is not None
    assert resp.status_code == 200
    assert resp.text == "<html>OK</html>"
    print("  [PASS] make_request success")


async def test_make_request_retry_on_error():
    """Test make_request retries on HTTP errors then returns None."""
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(
        side_effect=OSError("connection refused"),
    )
    mock_client.is_closed = False

    with patch(
        "esuls.request_cli._get_domain_client",
        return_value=mock_client,
    ):
        resp = await make_request(
            "https://example.com",
            max_attempt=2,
            exception_sleep=0,
            jitter=0,
        )

    assert resp is None
    assert mock_client.request.call_count == 2
    print("  [PASS] make_request retry on error")


async def test_make_request_force_response():
    """Test make_request returns response on failure when force_response=True."""
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.headers = {}
    mock_response.content = b"error"
    mock_response.text = "error"
    mock_response.url = "https://example.com"

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.is_closed = False

    with patch(
        "esuls.request_cli._get_domain_client",
        return_value=mock_client,
    ):
        resp = await make_request(
            "https://example.com",
            max_attempt=1,
            force_response=True,
            exception_sleep=0,
            jitter=0,
        )

    assert resp is not None
    assert resp.status_code == 500
    print("  [PASS] make_request force_response on failure")


async def test_make_request_no_retry_status():
    """Test make_request exits immediately for no-retry status codes."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.headers = {}
    mock_response.content = b"not found"
    mock_response.text = "not found"
    mock_response.url = "https://example.com"

    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    mock_client.is_closed = False

    with patch(
        "esuls.request_cli._get_domain_client",
        return_value=mock_client,
    ):
        resp = await make_request(
            "https://example.com",
            max_attempt=5,
            no_retry_status_codes=[404],
            force_response=True,
            exception_sleep=0,
            jitter=0,
        )

    assert resp is not None
    assert resp.status_code == 404
    assert mock_client.request.call_count == 1
    print("  [PASS] make_request no-retry status code")


async def test_close_shared_client():
    """Test close_shared_client cleans up the current loop's clients."""
    mock_client = AsyncMock()
    mock_client.is_closed = False

    state = _req_loop_state()
    state["domain_clients"]["https://a.com:h2:nv"] = mock_client
    try:
        await close_shared_client()
        mock_client.aclose.assert_awaited_once()
        assert "https://a.com:h2:nv" not in state["domain_clients"]
    finally:
        # Defensive: ensure no leak even if assertion fails
        state["domain_clients"].pop("https://a.com:h2:nv", None)
    print("  [PASS] close_shared_client")


async def test_make_request_cffi_mocked():
    """Test make_request_cffi with mocked curl_cffi session."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/html"}
    mock_response.content = b"<html>cffi</html>"
    mock_response.text = "<html>cffi</html>"
    mock_response.url = "https://example.com"

    mock_session = MagicMock()
    mock_session.request = AsyncMock(return_value=mock_response)

    with patch(
        "esuls.request_cli._get_session_cffi",
        return_value=mock_session,
    ):
        result = await make_request_cffi("https://example.com")

    assert result is not None
    assert result.status_code == 200
    assert result.text == "<html>cffi</html>"
    print("  [PASS] make_request_cffi success")


async def test_make_request_cffi_error():
    """Test make_request_cffi returns None on error."""
    mock_session = MagicMock()
    mock_session.request = AsyncMock(side_effect=OSError("fail"))

    with patch(
        "esuls.request_cli._get_session_cffi",
        return_value=mock_session,
    ):
        result = await make_request_cffi(
            "https://example.com",
            max_attempt=1,
            exception_sleep=0,
            jitter=0,
        )

    assert result is None
    print("  [PASS] make_request_cffi returns None on error")


if __name__ == "__main__":

    async def run_all_tests():
        print("\n" + "=" * 60)
        print("REQUEST CLI TESTS")
        print("=" * 60)

        tests = [
            ("Response object", test_response_object),
            ("Response JSON error", test_response_json_error),
            ("Extract domain", test_extract_domain),
            ("Apply jitter", test_apply_jitter),
            ("UserAgent fallback", test_get_user_agent_fallback),
            ("make_request success", test_make_request_success),
            ("make_request retry", test_make_request_retry_on_error),
            ("make_request force_response", test_make_request_force_response),
            ("make_request no-retry status", test_make_request_no_retry_status),
            ("close_shared_client", test_close_shared_client),
            ("make_request_cffi success", test_make_request_cffi_mocked),
            ("make_request_cffi error", test_make_request_cffi_error),
        ]

        passed = 0
        failed = 0
        for name, test_fn in tests:
            try:
                await test_fn()
                passed += 1
            except Exception as e:
                print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
                failed += 1

        print("\n" + "=" * 60)
        print(f"RESULTS: {passed} passed, {failed} failed")
        if failed == 0:
            print("ALL TESTS PASSED!")
        print("=" * 60)

    asyncio.run(run_all_tests())
