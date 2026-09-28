"""
KCEX HTTP API Client
====================
Provides low-level HTTP communication with KCEX public and private endpoints.
Implemented using Python's standard urllib.request to ensure clean TLS handshakes
that bypass Cloudflare/WAF JA3 signature blocks on third-party libraries.

Features:
- Persistent session handling with CookieJar
- Public and private request routing
- Dynamic request signing via KCEXSigner
- JSON serialization/deserialization
- Informative, clean error handling
"""

import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
import gzip
import json
import logging
import time
from typing import Dict, Any, Optional
from kcex.config import KCEXConfig
from kcex.signer import KCEXSigner

logger = logging.getLogger("KCEXClient")


class KCEXAPIError(Exception):
    """Exception raised when KCEX returns an error response or HTTP error."""
    def __init__(self, code: Any, message: str, raw_response: Optional[Any] = None):
        self.code = code
        self.message = message
        self.raw_response = raw_response
        super().__init__(f"[KCEX Error {code}] {message}")


class KCEXClient:
    """
    HTTP client for KCEX REST API utilizing urllib.request.
    """

    def __init__(self, config: Optional[KCEXConfig] = None):
        self.config = config or KCEXConfig()
        self.signer = KCEXSigner(self.config)
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    def _build_url(self, endpoint: str, is_platform: bool = False) -> str:
        """
        Constructs the full URL for a given endpoint route.
        """
        base = self.config.platform_base_url if is_platform else self.config.fapi_base_url
        endpoint_clean = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        return f"{base}{endpoint_clean}"

    def request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        is_private: bool = False,
        is_platform: bool = False,
        max_retries: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Sends an HTTP request to KCEX with automatic retry and backoff on rate limits / network errors.

        Args:
            method (str): 'GET', 'POST', etc.
            endpoint (str): Endpoint path (e.g. '/contract/ticker').
            params (dict, optional): URL query parameters.
            json_data (dict/list, optional): Body payload for POST requests.
            is_private (bool): If True, signs the request using session credentials.
            is_platform (bool): If True, uses the platform API base URL (/api/platform).
            max_retries (int, optional): Max retries on rate limit (510/429) or transient errors. Defaults to 2 for GET.

        Returns:
            Dict[str, Any]: Parsed JSON response.

        Raises:
            KCEXAPIError: If the server returns an unhandled error code or message after retries.
        """
        url = self._build_url(endpoint, is_platform=is_platform)
        method = method.upper()

        if is_private:
            if not self.config.is_authenticated:
                raise KCEXAPIError(
                    code="AUTH_MISSING",
                    message="Private endpoint requires KCEX_AUTH_TOKEN. Please configure credentials."
                )
            headers = self.signer.sign_request(method=method, body=json_data)
        else:
            headers = self.signer.build_public_headers()
            if json_data is not None:
                headers["Content-Type"] = "application/json"

        # Append query parameters to URL
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None}
            if clean_params:
                query_str = urllib.parse.urlencode(clean_params)
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}{query_str}"

        # Prepare payload
        data_bytes = None
        if json_data is not None:
            if isinstance(json_data, (dict, list)):
                payload_str = json.dumps(json_data, separators=(',', ':'))
            else:
                payload_str = str(json_data)
            data_bytes = payload_str.encode('utf-8')

        retries = max_retries if max_retries is not None else (2 if method == "GET" else 0)
        attempt = 0

        while True:
            req = urllib.request.Request(url=url, data=data_bytes, headers=headers, method=method)
            logger.debug("Request: %s %s (attempt %d/%d)", method, url, attempt + 1, retries + 1)

            try:
                with self.opener.open(req, timeout=self.config.timeout) as resp:
                    raw_bytes = resp.read()
                    # Check for gzip compression
                    if resp.headers.get("Content-Encoding") == "gzip":
                        try:
                            raw_bytes = gzip.decompress(raw_bytes)
                        except Exception:
                            pass
                    text = raw_bytes.decode('utf-8', errors='replace')
            except urllib.error.HTTPError as e:
                if attempt < retries and (e.code == 429 or e.code in (500, 502, 503, 504, 520, 521, 522, 524)):
                    delay = 1.5 * (2 ** attempt)
                    logger.warning(
                        "KCEX HTTP %s on %s %s. Retrying in %.1fs (attempt %d/%d)...",
                        e.code, method, endpoint, delay, attempt + 1, retries
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                if is_private and method == "GET" and attempt == 0 and e.code == 401:
                    # Retry once with "{}" body signature in case backend hashed empty JSON object
                    headers = self.signer.sign_request(method=method, body="{}")
                    attempt += 1
                    continue
                try:
                    err_body = e.read().decode('utf-8', errors='replace')
                    err_json = json.loads(err_body)
                    msg = err_json.get("msg") or err_json.get("message") or f"HTTP {e.code}"
                    code = err_json.get("code") or e.code
                except Exception:
                    msg = f"HTTP Error {e.code}"
                    code = e.code
                    err_body = None
                raise KCEXAPIError(code=code, message=msg, raw_response=err_body)
            except urllib.error.URLError as e:
                if attempt < retries:
                    delay = 1.0 * (attempt + 1)
                    logger.warning(
                        "KCEX network error on %s %s (%s). Retrying in %.1fs (attempt %d/%d)...",
                        method, endpoint, e.reason, delay, attempt + 1, retries
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise KCEXAPIError(code="NETWORK_ERROR", message=f"Network error: {str(e.reason)}")
            except Exception as e:
                if attempt < retries:
                    delay = 1.0 * (attempt + 1)
                    logger.warning(
                        "KCEX request exception on %s %s (%s). Retrying in %.1fs (attempt %d/%d)...",
                        method, endpoint, e, delay, attempt + 1, retries
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise KCEXAPIError(code="REQUEST_FAILED", message=f"Request failed: {str(e)}")

            # Parse JSON response
            try:
                res_json = json.loads(text)
            except Exception as e:
                if text.strip().startswith("<"):
                    msg = "KCEX platform returned HTML (system may be in maintenance or blocking IP)"
                    raise KCEXAPIError(code="MAINTENANCE_HTML", message=msg, raw_response=text[:200])
                raise KCEXAPIError(code="INVALID_JSON", message=f"Failed to parse JSON: {str(e)}")

            # Verify business status code
            code = res_json.get("code")
            success = res_json.get("success")

            # Handle rate limit (Error 510 or 429) in business response
            is_rate_limit = (code in (510, 429)) or ("frequency limit" in str(res_json.get("msg", "")).lower())
            if is_rate_limit and attempt < retries:
                delay = 2.0 * (attempt + 1)
                logger.warning(
                    "KCEX rate limit [Code %s: %s] on %s %s. Backing off %.1fs (attempt %d/%d)...",
                    code, res_json.get("msg"), method, endpoint, delay, attempt + 1, retries
                )
                time.sleep(delay)
                attempt += 1
                continue

            if code not in (0, 200, None) and success is False:
                msg = res_json.get("msg") or res_json.get("message") or "Unknown error"
                raise KCEXAPIError(code=code, message=msg, raw_response=res_json)

            return res_json

    def get_public(self, endpoint: str, params: Optional[Dict[str, Any]] = None, is_platform: bool = False) -> Dict[str, Any]:
        """Convenience helper for public GET requests."""
        return self.request(method="GET", endpoint=endpoint, params=params, is_private=False, is_platform=is_platform)

    def post_public(self, endpoint: str, json_data: Optional[Any] = None, is_platform: bool = False) -> Dict[str, Any]:
        """Convenience helper for public POST requests."""
        return self.request(method="POST", endpoint=endpoint, json_data=json_data, is_private=False, is_platform=is_platform)

    def get_private(self, endpoint: str, params: Optional[Dict[str, Any]] = None, is_platform: bool = False) -> Dict[str, Any]:
        """Convenience helper for private authenticated GET requests."""
        return self.request(method="GET", endpoint=endpoint, params=params, is_private=True, is_platform=is_platform)

    def post_private(self, endpoint: str, json_data: Optional[Any] = None, is_platform: bool = False) -> Dict[str, Any]:
        """Convenience helper for private authenticated POST requests."""
        return self.request(method="POST", endpoint=endpoint, json_data=json_data, is_private=True, is_platform=is_platform)
