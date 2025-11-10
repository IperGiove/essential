from dataclasses import dataclass
from functools import lru_cache
from typing import TypeAlias, Union, Optional, Dict, Any, TypeVar, AsyncContextManager, Literal
import asyncio
import json
import ssl
from loguru import logger
import httpx
from fake_useragent import UserAgent
from curl_cffi.requests import AsyncSession

# Type definitions optimized
ResponseT = TypeVar('ResponseT', bound='Response')
JsonType: TypeAlias = Dict[str, Any]
FileData: TypeAlias = tuple[str, Union[bytes, str], str]
Headers: TypeAlias = Dict[str, str]
HttpMethod: TypeAlias = Literal["GET", "POST",
                                "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

# Global shared client with connection pooling to prevent "Too many open files" error
_shared_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

# Global cached UserAgent to prevent file descriptor exhaustion
_user_agent: Optional[UserAgent] = None
_user_agent_lock = asyncio.Lock()


async def _get_user_agent() -> str:
    """Get or create cached UserAgent instance to avoid file descriptor leaks"""
    global _user_agent
    async with _user_agent_lock:
        if _user_agent is None:
            try:
                _user_agent = UserAgent()
            except Exception as e:
                # Fallback to a static user agent if UserAgent() fails
                logger.warning(
                    f"Failed to initialize UserAgent, using fallback: {e}")
                return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

        try:
            return _user_agent.random
        except Exception as e:
            logger.warning(
                f"Failed to get random user agent, using fallback: {e}")
            return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


@lru_cache(maxsize=1)
def _create_optimized_ssl_context() -> ssl.SSLContext:
    """Create an SSL context optimized for performance"""
    ctx = ssl._create_default_https_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(['http/1.1'])
    ctx.post_handshake_auth = True
    return ctx


async def _get_shared_client() -> httpx.AsyncClient:
    """Get or create shared HTTP client with connection pooling"""
    global _shared_client
    async with _client_lock:
        if _shared_client is None or _shared_client.is_closed:
            _shared_client = httpx.AsyncClient(
                verify=_create_optimized_ssl_context(),
                timeout=60,
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=50,
                    keepalive_expiry=30.0
                )
            )
        return _shared_client


@dataclass(frozen=True)
class Response:
    """Immutable response object with strong typing"""
    status_code: int
    headers: Headers
    _content: bytes
    text: str

    @property
    def content(self) -> bytes:
        return self._content

    def json(self) -> JsonType:
        return json.loads(self.text)


class AsyncRequest(AsyncContextManager['AsyncRequest']):
    def __init__(self) -> None:
        # self._logger = logging.getLogger(__name__)
        self._ssl_context = self._create_optimized_ssl_context()
        self._client: Optional[httpx.AsyncClient] = None

    @staticmethod
    @lru_cache(maxsize=1)
    def _create_optimized_ssl_context() -> ssl.SSLContext:
        """Create an SSL context optimized for performance"""
        ctx = ssl._create_default_https_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_alpn_protocols(['http/1.1'])
        ctx.post_handshake_auth = True
        return ctx

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
                verify=self._ssl_context,
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
                    text=httpx_response.text
                )

                # Handle unsuccessful status codes
                if response.status_code not in range(200, 300):
                    logger.warning(
                        f"Request: {response.status_code}\n"
                        f"Attempt {attempt}/{max_attempt}\n"
                        f"Url: {url}\n"
                        f"Params: {params}\n"
                        f"Response: {response.text[:1000]}\n"
                        f"Request data: {json_data}\n"
                    )
                    if skip_response:
                        patterns = [skip_response] if isinstance(
                            skip_response, str) else skip_response
                        # Skip if patterns list is empty
                        if patterns and any(pattern in response.text for pattern in patterns if pattern):
                            return response if force_response else None

                    if attempt + 1 == max_attempt:
                        return response if force_response else None

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

            except Exception as e:
                logger.error(
                    f"Request error: {e} - {url} - attempt {attempt + 1}/{max_attempt}")
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


async def make_request(
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
    add_user_agent: bool = False,
) -> Optional[Response]:
    """Main function to execute HTTP requests using shared client for connection reuse"""
    # Use shared client to avoid "Too many open files" error
    client = await _get_shared_client()

    # Prepare headers
    request_headers = dict(headers or {})
    if add_user_agent:
        request_headers["User-Agent"] = await _get_user_agent()

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
            httpx_response = await client.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                files=files_dict,
                headers=request_headers,
                timeout=timeout_request,
            )

            # Create custom Response object
            response = Response(
                status_code=httpx_response.status_code,
                headers=dict(httpx_response.headers),
                _content=httpx_response.content,
                text=httpx_response.text
            )

            # Handle unsuccessful status codes
            if response.status_code not in range(200, 300):
                logger.warning(
                    f"Request: {response.status_code}\n"
                    f"Attempt {attempt}/{max_attempt}\n"
                    f"Url: {url}\n"
                    f"Params: {params}\n"
                    f"Response: {response.text[:1000]}\n"
                    f"Request data: {json_data}\n"
                )
                if skip_response:
                    patterns = [skip_response] if isinstance(
                        skip_response, str) else skip_response
                    if patterns and any(pattern in response.text for pattern in patterns if pattern):
                        return response if force_response else None

                if attempt + 1 == max_attempt:
                    return response if force_response else None

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

        except Exception as e:
            logger.error(
                f"Request error: {e} - {url} - attempt {attempt + 1}/{max_attempt}")
            if attempt + 1 == max_attempt:
                return None
            await asyncio.sleep(exception_sleep)
            continue

    return None


@lru_cache(maxsize=1)
def _get_session_cffi() -> AsyncSession:
    """Cached session factory with optimized settings."""
    return AsyncSession(
        impersonate="chrome",
        timeout=30.0,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; Scraper)'}
    )


async def make_request_cffi(url: str) -> Optional[str]:
    """Optimized HTTP client with connection reuse and error handling."""
    try:
        response = await _get_session_cffi().get(url)
        print(response)
        response.raise_for_status()
        return response.text
    except Exception:
        return None


async def test_proxy():
    async with httpx.AsyncClient(proxy="http://0ce896d23159e7829ffc__cr.us:e4ada3ce93ad55ca@gw.dataimpesulse.com:823", timeout=10, verify=False) as client:
        try:
            r = await client.get("https://api.geckoterminal.com/api/v2/networks/zora-network/trending_pools?include=base_token%2C%20quote_token%2C%20dex&page=1")
            print(f"Proxy test: {r.status_code} {r.text}")
        except Exception as e:
            print(f"Proxy test failed: {e}")


async def test_make_request_cffi():
    url = "https://gmgn.ai/eth/token/0xeee2a64ae321964f969299ced0f4fcadcb0a1141"
    r = await make_request_cffi(url)
    print(r)

if __name__ == "__main__":
    # asyncio.run(make_request("https://api.geckoterminal.com/api/v2/networks/zora-network/trending_pools?include=base_token%2C%20quote_token%2C%20dex&page=1", method="GET"))
    # asyncio.run(test_proxy())
    asyncio.run(test_make_request_cffi())
