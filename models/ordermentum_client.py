import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from odoo import fields
import requests
from requests.exceptions import HTTPError, RequestException, Timeout


_logger = logging.getLogger(__name__)


def parse_ordermentum_datetime(value):
    if not value:
        return False

    if isinstance(value, str):
        raw = value.strip()
        dt = None
        try:
            dt = fields.Datetime.from_string(raw)
        except Exception:
            pass

        if dt is None:
            try:
                iso = raw
                if iso.endswith("Z"):
                    iso = iso[:-1] + "+00:00"
                dt = datetime.fromisoformat(iso)
            except Exception:
                return False
    elif isinstance(value, datetime):
        dt = value
    else:
        return False

    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    return dt

class OrdermentumAPIError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None, response_data: Optional[dict] = None):
        self.message = message
        self.status_code = status_code
        self.response_data = response_data
        super().__init__(self.message)


class OrdermentumClient:
    def __init__(self, env, timeout: int = None):
        self.env = env
        self.timeout = timeout or 120

    def _get_param(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.env["ir.config_parameter"].sudo().get_param(key, default=default)

    def _set_param(self, key: str, value: str) -> None:
        self.env["ir.config_parameter"].sudo().set_param(key, value)

    def _get_auth_base_url(self) -> str:
        base = self._get_param("cs_ordermentum_connector.auth_base_url", default="https://app.ordermentum.com")
        if not base:
            raise OrdermentumAPIError("Missing Ordermentum auth base URL (cs_ordermentum_connector.auth_base_url)")
        return base.rstrip("/")

    def _get_api_base_url(self) -> str:
        base = self._get_param("cs_ordermentum_connector.api_base_url", default="https://app.ordermentum.com")
        if not base:
            raise OrdermentumAPIError("Missing Ordermentum API base URL (cs_ordermentum_connector.api_base_url)")
        return base.rstrip("/")

    def _get_username(self) -> str:
        username = self._get_param("cs_ordermentum_connector.username")
        if not username:
            raise OrdermentumAPIError("Missing Ordermentum username (cs_ordermentum_connector.username)")
        return username

    def _get_password(self) -> str:
        password = self._get_param("cs_ordermentum_connector.password")
        if not password:
            raise OrdermentumAPIError("Missing Ordermentum password (cs_ordermentum_connector.password)")
        return password

    def _token_url(self) -> str:
        return f"{self._get_auth_base_url()}/v1/auth"

    def _get_cached_token(self) -> Optional[str]:
        token = self._get_param("cs_ordermentum_connector.access_token")
        expires_at_raw = self._get_param("cs_ordermentum_connector.access_token_expires_at")
        if not token or not expires_at_raw:
            return None

        try:
            expires_at = float(expires_at_raw)
        except (TypeError, ValueError):
            return None

        if time.time() >= expires_at:
            return None

        return token

    def _cache_token(self, access_token: str, expires_at: Optional[float] = None) -> None:
        self._set_param("cs_ordermentum_connector.access_token", access_token)
        if expires_at is None:
            expires_at = time.time() + 24 * 60 * 60 - 60
        self._set_param("cs_ordermentum_connector.access_token_expires_at", str(expires_at))

    def get_access_token(self, force_refresh: bool = False) -> str:
        if not force_refresh:
            cached = self._get_cached_token()
            if cached:
                return cached

        url = self._token_url()
        payload = {
            "username": self._get_username(),
            "password": self._get_password(),
        }

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(url=url, json=payload, headers=headers, timeout=self.timeout)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as e:
                raise OrdermentumAPIError(
                    f"Invalid JSON from auth endpoint: {response.text}",
                    status_code=response.status_code,
                ) from e

            access_token = data.get("access_token")
            if not access_token:
                raise OrdermentumAPIError(
                    f"Auth response missing access_token: {json.dumps(data)}",
                    status_code=response.status_code,
                    response_data=data,
                )

            self._cache_token(access_token=access_token)
            return access_token

        except Timeout as e:
            raise OrdermentumAPIError(f"Token request timeout: {str(e)}") from e
        except HTTPError as e:
            raise OrdermentumAPIError(
                f"Token request failed HTTP {e.response.status_code}: {e.response.text}",
                status_code=e.response.status_code,
            ) from e
        except RequestException as e:
            raise OrdermentumAPIError(f"Token request failed: {str(e)}") from e

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base = self._get_api_base_url()
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{base}{path}"

    def _parse_retry_after(self, response) -> Optional[float]:
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return None
        try:
            return float(retry_after)
        except (TypeError, ValueError):
            return None

    def request(self, method: str, path: str, *, headers: Optional[dict[str, str]] = None, **kwargs: Any) -> Any:
        token = self.get_access_token()

        request_headers = (headers or {}).copy()
        request_headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            }
        )

        url = self._build_url(path)

        max_retries = int(kwargs.pop("max_retries", 3))
        retry_no = 0
        backoff_seconds = 1.0

        def _do_request():
            return requests.request(
                method=method,
                url=url,
                headers=request_headers,
                timeout=kwargs.pop("timeout", self.timeout),
                **kwargs,
            )

        try:
            _logger.info(f"Ordermentum API Request: {method} {url}")
            response = _do_request()

            while response.status_code == 429 and retry_no < max_retries:
                retry_no += 1
                retry_after = self._parse_retry_after(response)
                sleep_s = retry_after if retry_after is not None else backoff_seconds
                _logger.warning(
                    "Ordermentum rate limit hit (429). Retry %s/%s after %ss: %s %s",
                    retry_no,
                    max_retries,
                    sleep_s,
                    method,
                    url,
                )
                time.sleep(max(0.0, sleep_s))
                if retry_after is None:
                    backoff_seconds = min(backoff_seconds * 2.0, 60.0)
                response = _do_request()

            if response.status_code == 401:
                token = self.get_access_token(force_refresh=True)
                request_headers["Authorization"] = f"Bearer {token}"
                response = _do_request()

            if response.status_code == 429:
                raise OrdermentumAPIError(
                    f"Rate limit exceeded (429) after retries: {method} {url}",
                    status_code=429,
                )

            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return response.json()

            return response.text

        except Timeout as e:
            raise OrdermentumAPIError(f"Request timeout: {method} {url} ({str(e)})") from e
        except HTTPError as e:
            raise OrdermentumAPIError(
                f"HTTP {e.response.status_code}: {e.response.text}",
                status_code=e.response.status_code,
            ) from e
        except RequestException as e:
            raise OrdermentumAPIError(f"Request failed: {str(e)}") from e
