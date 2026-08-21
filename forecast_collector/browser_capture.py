from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterable, Iterator


@dataclass(frozen=True)
class CapturedDocument:
    url: str
    content_type: str
    text: str


@dataclass
class BrowserCapture:
    requested_urls: list[str]
    documents: list[CapturedDocument] = field(default_factory=list)
    globals: list[Any] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    text_lines: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.documents or self.globals or self.tables or self.text_lines)


_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
)


def find_chromium_executable() -> str | None:
    configured = os.environ.get("EFC_CHROME_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    for candidate in _CHROME_CANDIDATES:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    for command in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "brave-browser",
    ):
        resolved = shutil.which(command)
        if resolved:
            return resolved
    return None


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return find_chromium_executable() is not None


def _clean_line(value: Any) -> str:
    text = " ".join(str(value or "").replace("\u00a0", " ").split())
    return text.strip()


def _browser_disabled() -> bool:
    value = os.environ.get("EFC_RTWH_BROWSER_FALLBACK", "1").strip().casefold()
    return value in {"0", "false", "no", "off", "disable", "disabled"}


def _writable_playwright_temp_root() -> Path:
    """Return a writable temp root without trusting the OS-provided TMPDIR.

    Some macOS accounts retain an unusable per-user /var/folders TMPDIR. Node
    (and therefore Playwright) trusts that path even when Python has already
    fallen back to /tmp. Prefer an explicit collector override, then a private
    cache directory under the current user's home.
    """

    candidates: list[Path] = []
    configured = os.environ.get("EFC_BROWSER_TMPDIR", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())

    cache_home = os.environ.get("XDG_CACHE_HOME", "").strip()
    if cache_home:
        candidates.append(Path(cache_home).expanduser() / "f_collector" / "playwright")

    try:
        home = Path.home()
    except Exception:
        home = None
    if home is not None:
        candidates.extend((
            home / ".cache" / "f_collector" / "playwright",
            home / ".f_collector_tmp" / "playwright",
        ))

    uid = str(os.getuid()) if hasattr(os, "getuid") else "user"
    candidates.append(Path("/tmp") / f"f_collector-playwright-{uid}")

    failures: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        probe = candidate / f".write-probe-{os.getpid()}"
        try:
            candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not candidate.is_dir():
                raise NotADirectoryError(str(candidate))
            probe.write_bytes(b"ok")
            probe.unlink()
            return candidate.resolve()
        except Exception as exc:
            try:
                probe.unlink(missing_ok=True)
            except Exception:
                pass
            failures.append(f"{candidate}: {type(exc).__name__}: {exc}")

    detail = "; ".join(failures) if failures else "no candidate paths were available"
    raise OSError(f"no writable Playwright temporary directory: {detail}")


@contextmanager
def _playwright_temp_environment() -> Iterator[Path]:
    """Temporarily make Python and Playwright use the same writable temp root."""

    root = _writable_playwright_temp_root()
    previous_env = {name: os.environ.get(name) for name in ("TMPDIR", "TMP", "TEMP")}
    previous_tempdir = tempfile.tempdir
    for name in previous_env:
        os.environ[name] = str(root)
    tempfile.tempdir = str(root)
    try:
        yield root
    finally:
        tempfile.tempdir = previous_tempdir
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def capture_public_pages(
    urls: Iterable[str],
    *,
    timeout_seconds: float = 120.0,
    settle_milliseconds: int = 6_000,
    max_document_bytes: int = 30 * 1024 * 1024,
) -> BrowserCapture:
    """Render public pages and retain text-like network responses.

    Infogram can connect a published chart to a live JSON/database source. In
    that mode the static embed HTML contains the project shell while the current
    table is delivered only after JavaScript runs. This optional fallback uses
    an already-installed Chromium-family browser and Playwright's Python driver
    to observe those same public responses. It does not log in, click through an
    access control, or use private Infogram APIs.
    """

    requested = [str(url).strip() for url in urls if str(url).strip()]
    result = BrowserCapture(requested_urls=requested)
    if not requested:
        return result
    if _browser_disabled():
        result.warnings.append("browser fallback disabled by EFC_RTWH_BROWSER_FALLBACK")
        return result

    executable = find_chromium_executable()
    if not executable:
        result.warnings.append(
            "no Chrome/Chromium executable found; set EFC_CHROME_EXECUTABLE to enable live-data capture"
        )
        return result
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        result.warnings.append(
            f"Playwright is not installed in the collector environment: {type(exc).__name__}: {exc}"
        )
        return result

    seen_documents: set[tuple[str, str, str]] = set()
    seen_lines: set[str] = set()
    seen_tables: set[str] = set()

    def add_document(url: str, content_type: str, body: bytes) -> None:
        if not body or len(body) > max_document_bytes:
            return
        lowered_type = content_type.casefold()
        allowed_type = any(
            token in lowered_type
            for token in (
                "json",
                "javascript",
                "text/",
                "csv",
                "xml",
                "html",
                "octet-stream",
            )
        )
        if not allowed_type and not body.lstrip().startswith((b"{", b"[", b"<")):
            return
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            return
        signal = text.casefold()
        # Keep project/data responses while dropping large generic JS bundles.
        if "javascript" in lowered_type and not any(
            token in signal
            for token in (
                "infographicdata",
                "chartdata",
                "sheetnames",
                "democrat",
                "republican",
                "district",
                "projected seats",
                "win probability",
            )
        ):
            return
        digest_key = (url, content_type, str(hash(text)))
        if digest_key in seen_documents:
            return
        seen_documents.add(digest_key)
        result.documents.append(CapturedDocument(url=url, content_type=content_type, text=text))

    def add_lines(text: str) -> None:
        for raw in text.splitlines():
            line = _clean_line(raw)
            if 2 <= len(line) <= 500 and line not in seen_lines:
                seen_lines.add(line)
                result.text_lines.append(line)

    def add_tables(tables: Any) -> None:
        if not isinstance(tables, list):
            return
        for table in tables:
            if not isinstance(table, list) or not table:
                continue
            rows: list[list[str]] = []
            for row in table:
                if not isinstance(row, list):
                    continue
                rows.append([_clean_line(cell) for cell in row])
            if len(rows) < 2:
                continue
            canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
            if canonical in seen_tables:
                continue
            seen_tables.add(canonical)
            result.tables.append(rows)

    try:
        with _playwright_temp_environment(), sync_playwright() as playwright:
            launch_args = ["--disable-dev-shm-usage", "--disable-background-networking"]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                launch_args.append("--no-sandbox")
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=executable,
                args=launch_args,
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 1200},
                java_script_enabled=True,
                ignore_https_errors=False,
            )
            page = context.new_page()

            def on_response(response: Any) -> None:
                try:
                    resource_type = response.request.resource_type
                    if resource_type not in {"document", "xhr", "fetch", "script"}:
                        return
                    content_type = response.headers.get("content-type", "")
                    body = response.body()
                    add_document(response.url, content_type, body)
                except Exception:
                    # Cross-origin and streaming responses are occasionally not
                    # readable through DevTools. They are non-fatal.
                    return

            page.on("response", on_response)
            for url in requested:
                try:
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=max(1_000, int(timeout_seconds * 1_000)),
                    )
                    page.wait_for_timeout(max(0, int(settle_milliseconds)))

                    # Trigger lazy chart/table requests without clicking or
                    # interacting with the forecast controls.
                    previous_height = -1
                    stable_rounds = 0
                    for _ in range(140):
                        height = int(page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"))
                        current = int(page.evaluate("window.scrollY + window.innerHeight"))
                        if height <= current + 4:
                            stable_rounds += 1
                        else:
                            stable_rounds = 0
                            page.evaluate("window.scrollBy(0, Math.max(700, window.innerHeight * 0.8))")
                        if height == previous_height and stable_rounds >= 3:
                            break
                        previous_height = height
                        page.wait_for_timeout(120)
                    page.wait_for_timeout(1_500)

                    try:
                        globals_payload = page.evaluate(
                            """
                            () => {
                              const result = {};
                              for (const key of [
                                'infographicData', '__INITIAL_STATE__', '__NEXT_DATA__',
                                '__APOLLO_STATE__', '__PRELOADED_STATE__'
                              ]) {
                                try {
                                  const value = window[key];
                                  if (value !== undefined && value !== null) {
                                    JSON.stringify(value);
                                    result[key] = value;
                                  }
                                } catch (_) {}
                              }
                              return result;
                            }
                            """
                        )
                        if isinstance(globals_payload, dict) and globals_payload:
                            result.globals.append(globals_payload)
                    except Exception:
                        pass

                    try:
                        dom_tables = page.evaluate(
                            """
                            () => Array.from(document.querySelectorAll('table')).map(table =>
                              Array.from(table.querySelectorAll('tr')).map(row =>
                                Array.from(row.querySelectorAll('th,td')).map(cell =>
                                  (cell.innerText || cell.textContent || '').trim()
                                )
                              )
                            )
                            """
                        )
                        add_tables(dom_tables)
                    except Exception:
                        pass

                    try:
                        add_lines(page.locator("body").inner_text(timeout=10_000))
                    except Exception:
                        pass
                    try:
                        add_document(url, "text/html; rendered=1", page.content().encode("utf-8"))
                    except Exception:
                        pass
                except PlaywrightTimeoutError as exc:
                    result.warnings.append(f"browser timeout for {url}: {exc}")
                except PlaywrightError as exc:
                    result.warnings.append(f"browser error for {url}: {exc}")
                except Exception as exc:
                    result.warnings.append(f"browser capture failed for {url}: {type(exc).__name__}: {exc}")
            context.close()
            browser.close()
    except Exception as exc:
        result.warnings.append(f"unable to launch browser fallback: {type(exc).__name__}: {exc}")
    return result
