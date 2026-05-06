"""HTTP request utilities with connection pooling, retry logic, and browser impersonation."""
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeAlias, Union, Optional, Dict, Any, AsyncContextManager, Literal
from urllib.parse import urlparse
import asyncio
import json
import random
import ssl
import threading
import weakref
from loguru import logger
import httpx
from fake_useragent import UserAgent
from curl_cffi.requests import AsyncSession

# Type definitions
JsonType: TypeAlias = Dict[str, Any]
FileData: TypeAlias = tuple[str, Union[bytes, str], str]
Headers: TypeAlias = Dict[str, str]
HttpMethod: TypeAlias = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

# Constants
_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_SUCCESS_STATUS_RANGE = range(200, 300)


# ─── per-loop registry ────────────────────────────────────────────────────
#
# httpx.AsyncClient, curl_cffi.AsyncSession, and Playwright browsers are all
# bound to the event loop they were created in: reuse from another loop
# fails (asyncio.Lock raises 'bound to a different event loop'; httpx
# schedules futures on the dead loop and hangs/raises). Storing every
# loop-bound resource in a per-loop dict (keyed by the loop in a
# WeakKeyDictionary) lets a process safely call `asyncio.run()` more than
# once and lets each loop have its own pool.

_req_state_by_loop: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = weakref.WeakKeyDictionary()
_req_state_guard = threading.Lock()


def _req_loop_state() -> dict:
    """Return per-loop state: client lock+pool, cffi sessions, playwright."""
    loop = asyncio.get_running_loop()
    with _req_state_guard:
        state = _req_state_by_loop.get(loop)
        if state is None:
            state = {
                "client_lock": asyncio.Lock(),
                "domain_clients": {},                 # cache_key -> httpx.AsyncClient
                "cffi_lock": asyncio.Lock(),
                "cffi_sessions": {True: None, False: None},
                "playwright_lock": asyncio.Lock(),
                "playwright_browser": None,
                "playwright_instance": None,
            }
            _req_state_by_loop[loop] = state
        return state


# fake_useragent.UserAgent is a sync object with no event-loop binding,
# so its cache stays global and is guarded by a plain threading.Lock.
_user_agent_cache: Dict[str, Optional[UserAgent]] = {"instance": None}
_user_agent_lock = threading.Lock()


async def _get_user_agent() -> str:
    """Get or create cached UserAgent instance to avoid file descriptor leaks."""
    with _user_agent_lock:
        if _user_agent_cache["instance"] is None:
            try:
                _user_agent_cache["instance"] = UserAgent()
            except (OSError, IOError) as e:
                logger.warning(f"Failed to initialize UserAgent, using fallback: {e}")
                return _FALLBACK_USER_AGENT

        try:
            return _user_agent_cache["instance"].random
        except (AttributeError, IndexError) as e:
            logger.warning(f"Failed to get random user agent, using fallback: {e}")
            return _FALLBACK_USER_AGENT


@lru_cache(maxsize=1)
def _create_optimized_ssl_context() -> ssl.SSLContext:
    """Create an SSL context optimized for performance"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(['http/1.1'])
    ctx.post_handshake_auth = True
    return ctx


def _extract_domain(url: str) -> str:
    """Extract domain from URL for connection pooling."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _apply_jitter(delay: float, jitter: float) -> float:
    """Add random jitter to delay to prevent thundering herd."""
    if jitter <= 0:
        return delay
    return delay + random.uniform(0, delay * jitter)


async def _get_domain_client(
    url: str,
    http2: bool = True,
    verify_ssl: bool = False,
) -> httpx.AsyncClient:
    """Get or create HTTP client for a specific domain with connection pooling.

    The pool is keyed by (domain, http2, verify_ssl) per running event loop,
    so callers that opt into real TLS verification get a separate client
    from the insecure default — and clients from a previous (dead) loop are
    never reused.
    """
    state = _req_loop_state()
    domain = _extract_domain(url)
    cache_key = f"{domain}:{'h2' if http2 else 'h1'}:{'v' if verify_ssl else 'nv'}"
    async with state["client_lock"]:
        clients: Dict[str, httpx.AsyncClient] = state["domain_clients"]
        if cache_key not in clients or clients[cache_key].is_closed:
            verify: Union[bool, ssl.SSLContext] = (
                True if verify_ssl else _create_optimized_ssl_context()
            )
            clients[cache_key] = httpx.AsyncClient(
                verify=verify,
                timeout=60,
                follow_redirects=True,
                http2=http2,
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30.0
                )
            )
        return clients[cache_key]


@dataclass(frozen=True)
class Response:
    """Immutable response object with strong typing"""
    status_code: int
    headers: Headers
    _content: bytes
    text: str
    url: str = ""  # final URL after redirects

    @property
    def content(self) -> bytes:
        """Return the raw response body as bytes."""
        return self._content

    def json(self) -> JsonType:
        """Parse the response text as JSON."""
        return json.loads(self.text)


class AsyncRequest(AsyncContextManager['AsyncRequest']):
    """Context manager for HTTP requests with automatic client lifecycle."""

    def __init__(self, verify_ssl: bool = False) -> None:
        # When verify_ssl is False (default) we use the optimized-but-insecure
        # context so the AsyncRequest behaves like make_request's pooled path.
        # Pass verify_ssl=True to enforce real certificate validation.
        self._verify: Union[bool, ssl.SSLContext] = (
            True if verify_ssl else _create_optimized_ssl_context()
        )
        self._client: Optional[httpx.AsyncClient] = None

    async def request(
        self,
        url: str,
        method: HttpMethod = "GET",
        headers: Optional[Headers] = None,
        cookies: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[JsonType] = None,
        files: Optional[Dict[str, FileData]] = None,
        proxy: Optional[str] = None,
        timeout_request: int = 60,
        max_attempt: int = 10,
        force_response: bool = False,
        json_response: bool = False,
        json_response_check: Optional[str] = None,
        skip_response: Optional[Union[str, list[str]]] = None,
        exception_sleep: float = 10,
        add_user_agent: bool = False
    ) -> Optional[Response]:
        """Execute an HTTP request with type handling and automatic retry"""
        # Prepare headers
        request_headers = dict(headers or {})
        if add_user_agent:
            request_headers["User-Agent"] = await _get_user_agent()

        # Initialize client if not already done
        if self._client is None:
            self._client = httpx.AsyncClient(
                verify=self._verify,
                timeout=timeout_request,
                cookies=cookies,
                headers=request_headers,
                proxy=proxy,
                follow_redirects=True,
                # http2=True  # Enable HTTP/2 for better performance
            )

        # Prepare files for multipart/form-data
        files_dict = None
        if files:
            files_dict = {}
            for field_name, (filename, content, content_type) in files.items():
                files_dict[field_name] = (filename, content, content_type)

        if params:
            params = {k: v for k, v in params.items() if v}
        for attempt in range(max_attempt):
            try:
                # Execute request with all necessary parameters
                httpx_response = await self._client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    files=files_dict,
                )

                # Create custom Response object
                response = Response(
                    status_code=httpx_response.status_code,
                    headers=dict(httpx_response.headers),
                    _content=httpx_response.content,
                    text=httpx_response.text,
                    url=str(httpx_response.url),
                )

                # Handle unsuccessful status codes
                if response.status_code not in _SUCCESS_STATUS_RANGE:
                    logger.warning(
                        f"Request: {response.status_code}\n"
                        f"Attempt {attempt + 1}/{max_attempt}\n"
                        f"Url: {url}\n"
                        f"Params: {params}\n"
                        f"Response: {response.text[:1000]}\n"
                        f"Request data: {json_data}\n"
                    )
                    if skip_response:
                        patterns = (
                            [skip_response]
                            if isinstance(skip_response, str)
                            else skip_response
                        )
                        if patterns and any(
                            pattern in response.text
                            for pattern in patterns if pattern
                        ):
                            return response if force_response else None

                    if attempt + 1 == max_attempt:
                        return response if force_response else None

                    # Exponential backoff for 429 (rate limit)
                    if response.status_code == 429:
                        backoff = min(
                            120.0, exception_sleep * (2 ** attempt)
                        )
                        logger.info(
                            f"Rate limited (429), backing off "
                            f"for {backoff:.1f}s"
                        )
                        await asyncio.sleep(backoff)
                    else:
                        await asyncio.sleep(exception_sleep)
                    continue

                # Validate JSON response
                if json_response:
                    try:
                        data = response.json()
                        if json_response_check and json_response_check not in data:
                            if attempt + 1 == max_attempt:
                                return None
                            await asyncio.sleep(exception_sleep)
                            continue
                    except json.JSONDecodeError:
                        if attempt + 1 == max_attempt:
                            return None
                        await asyncio.sleep(exception_sleep)
                        continue

                return response

            except (httpx.HTTPError, OSError) as e:
                logger.error(f"Request error: {e} - {url} - attempt {attempt + 1}/{max_attempt}")
                if attempt + 1 == max_attempt:
                    return None
                await asyncio.sleep(exception_sleep)
                continue

        return None

    async def __aenter__(self) -> 'AsyncRequest':
        """Context manager entry point"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit point"""
        if self._client:
            await self._client.aclose()
            self._client = None


async def close_shared_client() -> None:
    """Close all per-loop resources (HTTP clients, cffi sessions, playwright browser)
    bound to the *current* event loop. Resources owned by other loops (if any) are
    untouched and will be GC'd with the loop they belong to.
    """
    state = _req_loop_state()
    async with state["client_lock"]:
        for client in list(state["domain_clients"].values()):
            if not client.is_closed:
                await client.aclose()
        state["domain_clients"].clear()
    async with state["cffi_lock"]:
        for verify_flag, session in list(state["cffi_sessions"].items()):
            if session is not None:
                await session.close()
                state["cffi_sessions"][verify_flag] = None
    async with state["playwright_lock"]:
        if state["playwright_browser"] is not None:
            await state["playwright_browser"].close()
            state["playwright_browser"] = None
        if state["playwright_instance"] is not None:
            await state["playwright_instance"].stop()
            state["playwright_instance"] = None


async def close_domain_client(url: str, http2: Optional[bool] = None) -> None:
    """Close pooled HTTP client(s) for a specific domain in the current loop.

    Closes every cached variant (verify on/off) for the requested protocol(s);
    if http2 is None, both h1 and h2 variants are closed.
    """
    state = _req_loop_state()
    domain = _extract_domain(url)
    async with state["client_lock"]:
        clients: Dict[str, httpx.AsyncClient] = state["domain_clients"]
        if http2 is None:
            prefixes = (f"{domain}:h1:", f"{domain}:h2:")
        else:
            prefixes = (f"{domain}:{'h2' if http2 else 'h1'}:",)

        keys_to_close = [
            k for k in list(clients.keys())
            if k.startswith(prefixes)
        ]

        for key in keys_to_close:
            if not clients[key].is_closed:
                await clients[key].aclose()
            del clients[key]


async def make_request(
    url: str,
    method: HttpMethod = "GET",
    headers: Optional[Headers] = None,
    cookies: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_data: Optional[JsonType] = None,
    files: Optional[Dict[str, FileData]] = None,
    data: Optional[Union[str, bytes]] = None,
    form_data: Optional[Dict[str, Any]] = None,
    proxy: Optional[str] = None,
    timeout_request: int = 60,
    max_attempt: int = 10,
    force_response: bool = False,
    json_response: bool = False,
    json_response_check: Optional[str] = None,
    skip_response: Optional[Union[str, list[str]]] = None,
    exception_sleep: float = 10,
    add_user_agent: bool = False,
    follow_redirects: bool = True,
    verify_ssl: bool = False,
    no_retry_status_codes: Optional[list[int]] = None,
    http2: bool = True,
    jitter: float = 0.1,
) -> Optional[Response]:
    """Execute HTTP requests using per-domain client for connection reuse."""
    # Use dedicated client if proxy is specified, otherwise use per-domain pooled client
    own_client = None
    if proxy:
        ssl_context = _create_optimized_ssl_context() if not verify_ssl else True
        own_client = httpx.AsyncClient(
            verify=ssl_context,
            timeout=timeout_request,
            follow_redirects=follow_redirects,
            proxy=proxy,
            http2=http2,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30.0
            )
        )
        client = own_client
    else:
        client = await _get_domain_client(url, http2=http2, verify_ssl=verify_ssl)

    # Prepare headers
    request_headers = headers.copy() if headers else {}
    if add_user_agent:
        request_headers["User-Agent"] = await _get_user_agent()

    # Prepare files for multipart/form-data
    files_dict = {
        field_name: (filename, content, content_type)
        for field_name, (filename, content, content_type) in files.items()
    } if files else None

    # Filter empty params
    if params:
        params = {k: v for k, v in params.items() if v}

    # Determine data payload: form_data takes precedence over raw data
    request_data = form_data if form_data else data

    try:
        for attempt in range(max_attempt):
            try:
                # Execute request with all necessary parameters
                httpx_response = await client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    files=files_dict,
                    headers=request_headers,
                    timeout=timeout_request,
                    data=request_data,
                    cookies=cookies,
                    follow_redirects=follow_redirects,
                )

                # Create custom Response object
                response = Response(
                    status_code=httpx_response.status_code,
                    headers=dict(httpx_response.headers),
                    _content=httpx_response.content,
                    text=httpx_response.text,
                    url=str(httpx_response.url),
                )

                # Handle unsuccessful status codes
                if response.status_code not in _SUCCESS_STATUS_RANGE:
                    logger.debug(
                        f"Request: {response.status_code}\n"
                        f"Attempt {attempt + 1}/{max_attempt}\n"
                        f"Url: {url}\n"
                        f"Params: {params}\n"
                        f"Response: {response.text[:1000]}\n"
                        f"Request data: {json_data}\n"
                    )

                    # Exit immediately for specific status codes (no retry)
                    if (no_retry_status_codes
                            and response.status_code in no_retry_status_codes):
                        return response if force_response else None

                    if skip_response:
                        patterns = (
                            [skip_response]
                            if isinstance(skip_response, str)
                            else skip_response
                        )
                        if patterns and any(
                            pattern in response.text
                            for pattern in patterns if pattern
                        ):
                            return response if force_response else None

                    if attempt + 1 == max_attempt:
                        return response if force_response else None

                    # Exponential backoff for 429 (rate limit)
                    if response.status_code == 429:
                        backoff = min(120.0, exception_sleep * (2 ** attempt))
                        logger.debug(f"Rate limited (429), backing off for {backoff:.1f}s")
                        await asyncio.sleep(_apply_jitter(backoff, jitter))
                    else:
                        await asyncio.sleep(_apply_jitter(exception_sleep, jitter))
                    continue

                # Validate JSON response
                if json_response:
                    try:
                        response_data = response.json()
                        if json_response_check and json_response_check not in response_data:
                            if attempt + 1 == max_attempt:
                                return None
                            await asyncio.sleep(_apply_jitter(exception_sleep, jitter))
                            continue
                    except json.JSONDecodeError:
                        if attempt + 1 == max_attempt:
                            return None
                        await asyncio.sleep(_apply_jitter(exception_sleep, jitter))
                        continue

                return response

            except (httpx.HTTPError, OSError) as e:
                logger.debug(f"Request error: {e} - {url} - attempt {attempt + 1}/{max_attempt}")
                if attempt + 1 == max_attempt:
                    return None
                await asyncio.sleep(_apply_jitter(exception_sleep, jitter))
                continue

        return None
    finally:
        if own_client:
            await own_client.aclose()


async def _get_session_cffi(verify_ssl: bool = False) -> AsyncSession:
    """Get or create cached curl_cffi session for the current event loop.

    Two sessions are cached per loop (verify on/off) so callers that opt
    into TLS verification get a separate session from the insecure default.
    """
    state = _req_loop_state()
    async with state["cffi_lock"]:
        sessions: Dict[bool, Optional[AsyncSession]] = state["cffi_sessions"]
        if sessions[verify_ssl] is None:
            sessions[verify_ssl] = AsyncSession(
                impersonate="chrome",
                timeout=30,
                verify=verify_ssl,
            )
        return sessions[verify_ssl]


async def make_request_cffi(
    url: str,
    method: HttpMethod = "GET",
    headers: Optional[Headers] = None,
    cookies: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    json_data: Optional[JsonType] = None,
    data: Optional[Union[str, bytes]] = None,
    timeout_request: int = 30,
    max_attempt: int = 3,
    force_response: bool = False,
    no_retry_status_codes: Optional[list[int]] = None,
    exception_sleep: float = 5,
    jitter: float = 0.1,
    verify_ssl: bool = False,
) -> Optional[Response]:
    """HTTP client using curl_cffi for browser TLS impersonation.

    Falls back to this when standard httpx is blocked by TLS fingerprinting
    (Cloudflare, Wix, Akamai, etc.). Returns the same Response object as
    make_request for drop-in compatibility.
    """
    session = await _get_session_cffi(verify_ssl=verify_ssl)

    for attempt in range(max_attempt):
        try:
            cffi_resp = await session.request(
                method=method,
                url=url,
                headers=headers,
                cookies=cookies,
                params=params,
                json=json_data,
                data=data,
                timeout=timeout_request,
            )

            response = Response(
                status_code=cffi_resp.status_code,
                headers=dict(cffi_resp.headers),
                _content=cffi_resp.content,
                text=cffi_resp.text,
                url=str(cffi_resp.url),
            )

            if response.status_code not in _SUCCESS_STATUS_RANGE:
                logger.debug(
                    f"cffi request: {response.status_code} - {url} "
                    f"- attempt {attempt + 1}/{max_attempt}"
                )

                if (no_retry_status_codes
                        and response.status_code in no_retry_status_codes):
                    return response if force_response else None

                if attempt + 1 == max_attempt:
                    return response if force_response else None

                if response.status_code == 429:
                    backoff = min(120.0, exception_sleep * (2 ** attempt))
                    await asyncio.sleep(_apply_jitter(backoff, jitter))
                else:
                    await asyncio.sleep(_apply_jitter(exception_sleep, jitter))
                continue

            return response

        except Exception as e:
            logger.debug(f"cffi request error: {e} - {url} - attempt {attempt + 1}/{max_attempt}")
            if attempt + 1 == max_attempt:
                return None
            await asyncio.sleep(_apply_jitter(exception_sleep, jitter))

    return None


async def _get_playwright_browser():
    """Get or create cached Playwright Chromium browser for the current loop."""
    state = _req_loop_state()
    async with state["playwright_lock"]:
        if state["playwright_browser"] is None:
            from playwright.async_api import async_playwright
            state["playwright_instance"] = await async_playwright().start()
            state["playwright_browser"] = await state["playwright_instance"].chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
        return state["playwright_browser"]


async def make_request_playwright(
    url: str,
    timeout_request: int = 15,
    max_attempt: int = 3,
    wait_seconds: float = 2,
    no_retry_status_codes: list[int] | None = None,
    force_response: bool = False,
    exception_sleep: float = 5,
    jitter: float = 0.1,
) -> Optional[Response]:
    """HTTP client using Playwright for JavaScript-rendered pages.

    Fully async — use this when standard httpx/curl_cffi return empty shells
    from Angular SPAs or other JS-heavy sites. Returns the same Response
    object for drop-in compatibility.
    """
    browser = await _get_playwright_browser()

    for attempt in range(max_attempt):
        page = None
        try:
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page.set_default_timeout(timeout_request * 1000)

            resp = await page.goto(url, wait_until="domcontentloaded")
            # Let JS render
            await page.wait_for_timeout(int(wait_seconds * 1000))

            page_source = await page.content()
            final_url = page.url
            status_code = resp.status if resp else 200

            response = Response(
                status_code=status_code,
                headers=dict(resp.headers) if resp else {},
                _content=page_source.encode("utf-8"),
                text=page_source,
                url=final_url,
            )

            if status_code not in _SUCCESS_STATUS_RANGE:
                logger.debug(
                    f"playwright request: {status_code} - {url} "
                    f"- attempt {attempt + 1}/{max_attempt}"
                )
                if (no_retry_status_codes
                        and status_code in no_retry_status_codes):
                    return response if force_response else None
                if attempt + 1 == max_attempt:
                    return response if force_response else None
                await asyncio.sleep(_apply_jitter(exception_sleep, jitter))
                continue

            return response

        except Exception as e:
            logger.debug(
                f"playwright request error: {e} - {url} "
                f"- attempt {attempt + 1}/{max_attempt}"
            )
            if attempt + 1 == max_attempt:
                return None
            await asyncio.sleep(_apply_jitter(exception_sleep, jitter))
        finally:
            if page:
                await page.close()

    return None