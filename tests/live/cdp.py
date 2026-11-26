"""A headless Chromium for the live suite, driven over the DevTools protocol.

The browser is the Chromium the browser tests already use
(:func:`tests.test_preview_renderers.chromium`), started on a debugging port in the
sandbox's port block with a throwaway profile. The suite signs people in over HTTP
(:class:`tests.live.client.Browser`) and hands their cookies to the page, so no
password or TOTP code is typed into it. Downloads land in a folder the suite names,
where the saved files are checked as files.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import httpx
from websockets.sync.client import ClientConnection, connect

from tests.live.client import Browser
from tests.test_preview_renderers import chromium, stop_browser

DESKTOP = (1400, 900)
PHONE = (390, 844)

#: Stands in for the function the served chat page still calls at start-up but no
#: script defines since 0.3.0rc56 (Skuld question 94, decision 260). Remove it with the
#: strict xfail in test_screens_live.py once the page is fixed.
CHAT_PAGE_SHIM = "window.configureMarked = window.configureMarked || function () {};"


class CdpError(AssertionError):
    pass


class Page:
    """One tab: navigate, evaluate, screenshot, and the events seen so far."""

    def __init__(self, socket: ClientConnection) -> None:
        self.socket = socket
        self.ids = itertools.count(1)
        self.events: list[dict[str, Any]] = []

    def send(self, method: str, params: dict[str, Any] | None = None, timeout: float = 120) -> Any:
        number = next(self.ids)
        self.socket.send(json.dumps({"id": number, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CdpError(f"{method}: no answer within {timeout:.0f}s")
            message = json.loads(self.socket.recv(timeout=left))
            if message.get("id") == number:
                if "error" in message:
                    raise CdpError(f"{method}: {message['error']}")
                return message.get("result", {})
            self.events.append(message)

    def evaluate(self, expression: str, timeout: float = 120) -> Any:
        """The value of ``expression`` (awaited when it is a promise)."""
        found = self.send(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        if "exceptionDetails" in found:
            raise CdpError(f"page error: {found['exceptionDetails']}")
        return found.get("result", {}).get("value")

    def wait(self, expression: str, timeout: float = 90, what: str = "") -> Any:
        """Poll ``expression`` in the page until it is truthy."""
        deadline = time.monotonic() + timeout
        value: Any = None
        while time.monotonic() < deadline:
            try:
                value = self.evaluate(expression)
            except CdpError:
                value = None
            if value:
                return value
            time.sleep(0.25)
        raise CdpError(f"{what or expression} within {timeout:.0f}s (last {value!r})")

    def size(self, width: int, height: int) -> None:
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": width < 600,
            },
        )

    def navigate(self, url: str, timeout: float = 120) -> None:
        self.send("Page.navigate", {"url": url})
        self.wait("document.readyState === 'complete'", timeout, f"{url} loaded")

    def screenshot(self, path: Path) -> Path:
        shot = self.send(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(shot["data"]))
        return path

    def errors(self) -> list[str]:
        """Uncaught exceptions the page reported (Runtime.exceptionThrown)."""
        return [
            str(e["params"]["exceptionDetails"].get("exception", {}).get("description", ""))
            for e in self.events
            if e.get("method") == "Runtime.exceptionThrown"
        ]


class Chromium:
    """The browser process, its DevTools endpoint and the folder downloads go to."""

    def __init__(self, port: int, downloads: Path) -> None:
        binary = chromium()
        assert binary, "no Chromium is installed (see tests/test_preview_renderers.py)"
        self.port = port
        self.downloads = downloads
        downloads.mkdir(parents=True, exist_ok=True)
        self.profile = tempfile.mkdtemp(prefix="spark-live-cdp-")
        self.process = subprocess.Popen(  # noqa: S603 - the installed Chromium, fixed argv
            [
                binary,
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-debugging-port={port}",
                "--remote-allow-origins=*",
                f"--user-data-dir={self.profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while True:
            try:
                version = httpx.get(f"{self.endpoint}/json/version", timeout=5).json()
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        self.sockets = ExitStack()
        self.browser_socket = self.sockets.enter_context(
            connect(version["webSocketDebuggerUrl"], max_size=None, open_timeout=30)
        )
        self.control = Page(self.browser_socket)
        self.control.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(downloads), "eventsEnabled": True},
        )
        self.pages: list[Page] = []

    def page(self) -> Page:
        target = httpx.put(f"{self.endpoint}/json/new?about:blank", timeout=30).json()
        socket = self.sockets.enter_context(
            connect(target["webSocketDebuggerUrl"], max_size=None, open_timeout=30)
        )
        page = Page(socket)
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Network.enable")
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.sockets.close()
        stop_browser(self.process)


def sign_in(page: Page, browser: Browser, theme: str) -> None:
    """Give the page ``browser``'s session cookies and the person's theme choice (saved
    for them, and in localStorage ``app-theme``, which the page reads first)."""
    for name, (value, path) in browser.jar.items():
        # SPARK's cookies carry the __Host- and __Secure- prefixes, which need Secure;
        # Chromium treats the loopback origin as secure, as the client does (D70).
        page.send(
            "Network.setCookie",
            {
                "name": name,
                "value": value,
                "url": browser.origin + path,
                "path": path,
                "secure": True,
                "httpOnly": True,
                "sameSite": "Strict",
            },
        )
    # The person's saved choice wins over the browser's copy (ui.js), so save it too.
    chosen = browser.post("/api/theme", json={"theme": theme})
    assert chosen.status_code == 200, (chosen.status_code, chosen.text[:300])
    script = f"try {{ localStorage.setItem('app-theme', '{theme}'); }} catch (e) {{}}"
    page.send("Page.addScriptToEvaluateOnNewDocument", {"source": script})


def click(page: Page, selector: str) -> None:
    """Click the first element ``selector`` finds, as a person would."""
    found = page.evaluate(
        f"(() => {{ const e = document.querySelector({json.dumps(selector)}); "
        "if (!e) return false; e.click(); return true; })()"
    )
    assert found, f"nothing matches {selector}"


def shim_chat_page(page: Page) -> None:
    """Install :data:`CHAT_PAGE_SHIM` before every document the page loads."""
    page.send("Page.addScriptToEvaluateOnNewDocument", {"source": CHAT_PAGE_SHIM})


def saved(downloads: Path, name: str, timeout: float = 60) -> Path:
    """The downloaded file ``name`` once Chromium has finished writing it."""
    target = downloads / name
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if target.is_file() and not (downloads / f"{name}.crdownload").exists():
            if target.stat().st_size > 0:
                return target
        time.sleep(0.25)
    raise CdpError(f"{name} was not downloaded into {downloads}: {os.listdir(downloads)}")


@contextmanager
def chromium_session(port: int, downloads: Path) -> Iterator[Chromium]:
    browser = Chromium(port, downloads)
    try:
        yield browser
    finally:
        browser.close()
