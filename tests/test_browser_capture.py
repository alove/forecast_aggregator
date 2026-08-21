import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from forecast_collector.browser_capture import (
    _playwright_temp_environment,
    capture_public_pages,
    playwright_available,
)


class BrowserTempDirectoryTests(unittest.TestCase):
    def test_bad_system_tmpdir_is_replaced_for_playwright_and_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked = root / "not-a-directory"
            blocked.write_text("blocked", encoding="utf-8")
            safe = root / "safe-playwright-temp"
            previous_tempdir = tempfile.tempdir
            with patch.dict(
                os.environ,
                {
                    "EFC_BROWSER_TMPDIR": str(safe),
                    "TMPDIR": str(blocked),
                    "TMP": str(blocked),
                    "TEMP": str(blocked),
                },
                clear=False,
            ):
                with _playwright_temp_environment() as selected:
                    self.assertEqual(selected, safe.resolve())
                    self.assertEqual(os.environ["TMPDIR"], str(safe.resolve()))
                    self.assertEqual(os.environ["TMP"], str(safe.resolve()))
                    self.assertEqual(os.environ["TEMP"], str(safe.resolve()))
                    self.assertEqual(tempfile.tempdir, str(safe.resolve()))
                    with tempfile.NamedTemporaryFile() as handle:
                        self.assertEqual(Path(handle.name).parent, safe.resolve())
                self.assertEqual(os.environ["TMPDIR"], str(blocked))
                self.assertEqual(os.environ["TMP"], str(blocked))
                self.assertEqual(os.environ["TEMP"], str(blocked))
                self.assertEqual(tempfile.tempdir, previous_tempdir)


@unittest.skipUnless(playwright_available(), "Chrome/Chromium plus Playwright are required")
class BrowserCaptureIntegrationTests(unittest.TestCase):
    def test_captures_public_fetch_json_and_rendered_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "feed.json").write_text(
                json.dumps({
                    "rows": [
                        {
                            "District": "NY-01",
                            "Democratic Win Probability": "61%",
                            "Republican Win Probability": "39%",
                        }
                    ]
                }),
                encoding="utf-8",
            )
            (root / "index.html").write_text(
                """
                <!doctype html><html><body><table id="forecast"></table>
                <script>
                fetch('/feed.json').then(r => r.json()).then(data => {
                  const table = document.getElementById('forecast');
                  table.innerHTML = '<tr><th>District</th><th>Democratic Win Probability</th><th>Republican Win Probability</th></tr>' +
                    data.rows.map(row => `<tr><td>${row.District}</td><td>${row['Democratic Win Probability']}</td><td>${row['Republican Win Probability']}</td></tr>`).join('');
                });
                </script></body></html>
                """,
                encoding="utf-8",
            )

            class QuietHandler(SimpleHTTPRequestHandler):
                def log_message(self, *_args):
                    return

            handler = lambda *args, **kwargs: QuietHandler(  # noqa: E731
                *args, directory=directory, **kwargs
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/index.html"
                capture = capture_public_pages(
                    [url], timeout_seconds=20, settle_milliseconds=300
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        if (
            not capture.available
            and any("ERR_BLOCKED_BY_ADMINISTRATOR" in warning for warning in capture.warnings)
        ):
            self.skipTest("browser networking is blocked by the test environment")
        self.assertTrue(capture.available, capture.warnings)
        feed_documents = [doc for doc in capture.documents if doc.url.endswith("feed.json")]
        self.assertTrue(feed_documents, capture.warnings)
        self.assertEqual(json.loads(feed_documents[0].text)["rows"][0]["District"], "NY-01")
        self.assertTrue(any(table and table[0][0] == "District" for table in capture.tables))


if __name__ == "__main__":
    unittest.main()
