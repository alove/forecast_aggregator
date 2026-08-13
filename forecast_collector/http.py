from __future__ import annotations

from dataclasses import dataclass
import json
import os
import random
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import FetchError


@dataclass(frozen=True)
class HttpResponse:
    url: str
    content: bytes
    content_type: str
    etag: str
    last_modified: str

    def text(self) -> str:
        return self.content.decode("utf-8-sig")

    def json(self) -> Any:
        return json.loads(self.text())


class HttpClient:
    def __init__(
        self,
        timeout: float = 30.0,
        retries: int = 3,
        max_response_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self.max_response_bytes = max(1, int(max_response_bytes))
        contact = os.environ.get("EFC_CONTACT_EMAIL", "").strip()
        suffix = f"; contact={contact}" if contact else ""
        self.user_agent = f"RhubarbElectionForecastCollector/1.0{suffix}"

    def get(self, url: str) -> HttpResponse:
        last_error: BaseException | None = None
        attempts = self.retries + 1
        for attempt in range(attempts):
            request = Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json,text/csv,text/plain,*/*;q=0.5",
                    "Cache-Control": "no-cache",
                },
                method="GET",
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    content = response.read(self.max_response_bytes + 1)
                    if len(content) > self.max_response_bytes:
                        raise FetchError(
                            f"response from {url} exceeds "
                            f"{self.max_response_bytes:,} bytes"
                        )
                    if not content:
                        raise FetchError(f"empty response from {url}")
                    return HttpResponse(
                        url=response.geturl(),
                        content=content,
                        content_type=response.headers.get("Content-Type", ""),
                        etag=response.headers.get("ETag", ""),
                        last_modified=response.headers.get("Last-Modified", ""),
                    )
            except HTTPError as exc:
                last_error = exc
                retryable = exc.code in {408, 425, 429, 500, 502, 503, 504}
                if not retryable or attempt == attempts - 1:
                    break
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = min(10.0, 2.0 ** attempt + random.random())
                time.sleep(delay)
            except (URLError, TimeoutError, OSError, FetchError) as exc:
                last_error = exc
                if attempt == attempts - 1:
                    break
                time.sleep(min(10.0, 2.0 ** attempt + random.random()))
        raise FetchError(f"failed to fetch {url} after {attempts} attempt(s): {last_error}")
