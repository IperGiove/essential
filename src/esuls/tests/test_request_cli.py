"""
Tests for request_cli HTTP utilities.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock

from esuls.request_cli import (
    Response,
    AsyncRequest,
    make_request,
    make_request_cffi,
    close_shared_client,
    _get_user_agent,
    _extract_domain,
    _apply_jitter,
    _get_domain_client,
    _req_loop_state,
    _run_with_retry,
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


def _make_mock_client(status_code=200, body=b"ok", text="ok"):
    """Build a MagicMock httpx.AsyncClient that records request kwargs."""
    captured = []

    async def fake_request(method, url, **kwargs):
        captured.append({"method": method, "url": url, **kwargs})
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.headers = {"content-type": "text/html"}
        mock_response.content = body
        mock_response.text = text
        mock_response.url = url
        return mock_response

    mock_client = MagicMock()
    mock_client.request = AsyncMock(side_effect=fake_request)
    mock_client.aclose = AsyncMock()
    mock_client.is_closed = False
    return mock_client, captured


async def test_async_request_per_call_headers_cookies():
    """REGRESSION: AsyncRequest must pass per-call headers/cookies on every
    call. Pre-fix the values from the FIRST call were baked into the client
    and subsequent calls' values were silently ignored.
    """
    mock_client, captured = _make_mock_client()

    with patch(
        "esuls.request_cli.httpx.AsyncClient",
        return_value=mock_client,
    ):
        async with AsyncRequest() as req:
            await req.request(
                "https://example.com",
                headers={"X-Call": "1"},
                cookies={"session": "first"},
                max_attempt=1, exception_sleep=0, jitter=0,
            )
            await req.request(
                "https://example.com",
                headers={"X-Call": "2"},
                cookies={"session": "second"},
                max_attempt=1, exception_sleep=0, jitter=0,
            )

    assert captured[0]["headers"] == {"X-Call": "1"}, captured
    assert captured[0]["cookies"] == {"session": "first"}, captured
    assert captured[1]["headers"] == {"X-Call": "2"}, captured
    assert captured[1]["cookies"] == {"session": "second"}, captured
    print("  [PASS] AsyncRequest applies per-call headers and cookies")


async def test_async_request_proxy_uses_one_shot_client():
    """When proxy is given, AsyncRequest creates and closes a one-shot client
    for that call only — the persistent instance client stays untouched.
    """
    persistent_client, _ = _make_mock_client()
    one_shot_client, captured = _make_mock_client()

    constructed = []

    def fake_constructor(*args, **kwargs):
        constructed.append(kwargs)
        # First call: persistent client (no proxy kwarg)
        # Second call: one-shot client (proxy kwarg present)
        return one_shot_client if "proxy" in kwargs else persistent_client

    with patch(
        "esuls.request_cli.httpx.AsyncClient",
        side_effect=fake_constructor,
    ):
        req = AsyncRequest()
        # First call uses persistent client (no proxy)
        await req.request(
            "https://example.com",
            max_attempt=1, exception_sleep=0, jitter=0,
        )
        # The one-shot must NOT be closed yet (only persistent has run)
        one_shot_client.aclose.assert_not_called()
        persistent_client.aclose.assert_not_called()

        # Second call uses one-shot client (proxy set)
        await req.request(
            "https://example.com",
            proxy="http://proxy:8080",
            max_attempt=1, exception_sleep=0, jitter=0,
        )
        # The one-shot is closed after its single use; persistent stays open.
        one_shot_client.aclose.assert_awaited_once()
        persistent_client.aclose.assert_not_called()

    # Two AsyncClient instances were constructed
    assert len(constructed) == 2, constructed
    assert "proxy" not in constructed[0]
    assert constructed[1].get("proxy") == "http://proxy:8080"
    print("  [PASS] AsyncRequest with proxy creates and closes a one-shot client")


async def test_async_request_no_retry_status_codes():
    """AsyncRequest must support no_retry_status_codes (was missing pre-refactor)."""
    mock_client, _ = _make_mock_client(status_code=404, body=b"nf", text="nf")

    with patch(
        "esuls.request_cli.httpx.AsyncClient",
        return_value=mock_client,
    ):
        async with AsyncRequest() as req:
            resp = await req.request(
                "https://example.com",
                max_attempt=5,
                no_retry_status_codes=[404],
                force_response=True,
                exception_sleep=0,
                jitter=0,
            )

    assert resp is not None and resp.status_code == 404
    # Without no_retry_status_codes the loop would have done 5 calls;
    # with it, it bails out after the first attempt.
    assert mock_client.request.call_count == 1, mock_client.request.call_count
    print("  [PASS] AsyncRequest honours no_retry_status_codes")


async def test_async_request_jitter_applied():
    """AsyncRequest must apply jitter on retry sleeps (was missing pre-refactor).

    We patch _apply_jitter and assert it gets called with the configured
    jitter when AsyncRequest retries on a 500.
    """
    mock_client, _ = _make_mock_client(status_code=500, body=b"err", text="err")

    with patch(
        "esuls.request_cli.httpx.AsyncClient",
        return_value=mock_client,
    ), patch(
        "esuls.request_cli._apply_jitter",
        wraps=_apply_jitter,
    ) as jitter_spy:
        async with AsyncRequest() as req:
            await req.request(
                "https://example.com",
                max_attempt=2,
                exception_sleep=0,
                jitter=0.42,
            )

    # _apply_jitter is called with (delay, 0.42) at least once on retry sleep.
    assert any(
        call.args[1] == 0.42 or call.kwargs.get("jitter") == 0.42
        for call in jitter_spy.call_args_list
    ), jitter_spy.call_args_list
    print("  [PASS] AsyncRequest applies jitter on retry sleeps")


async def test_async_request_persistent_client_reused():
    """Across many .request() calls without proxy, the same persistent
    httpx client is reused (the whole point of AsyncRequest)."""
    mock_client, captured = _make_mock_client()

    constructed = []

    def constructor(*args, **kwargs):
        constructed.append(kwargs)
        return mock_client

    with patch(
        "esuls.request_cli.httpx.AsyncClient",
        side_effect=constructor,
    ):
        async with AsyncRequest() as req:
            for _ in range(4):
                await req.request(
                    "https://example.com",
                    max_attempt=1, exception_sleep=0, jitter=0,
                )

    # Only ONE AsyncClient was constructed across 4 calls
    assert len(constructed) == 1, constructed
    assert mock_client.request.call_count == 4
    print("  [PASS] AsyncRequest reuses the persistent client across calls")


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
            ("AsyncRequest per-call headers/cookies",
             test_async_request_per_call_headers_cookies),
            ("AsyncRequest proxy one-shot",
             test_async_request_proxy_uses_one_shot_client),
            ("AsyncRequest no_retry_status_codes",
             test_async_request_no_retry_status_codes),
            ("AsyncRequest jitter applied",
             test_async_request_jitter_applied),
            ("AsyncRequest persistent client reused",
             test_async_request_persistent_client_reused),
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
