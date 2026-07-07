"""Browser-backed Amazon resolver (the auth half of the hybrid).

The Amazon track API is gated by a Cloudflare Turnstile JWT that is bound to the
minting browser's fingerprint, so the ``amz/track`` call cannot be replayed from
plain httpx (it 401s). This session mints the JWT once in a real headed Chromium
(Turnstile passes a genuine browser; the headless shell is flagged) and issues
the ``amz/track`` fetch from the page context, where the fingerprint matches.

It exposes ``fetch_amz(params) -> dict`` for the pure adapter to consume. The
signed CloudFront download + CENC decrypt happen outside the browser (no auth).

All I/O; excluded from coverage. Pure parsing lives in the adapter.
"""

from __future__ import annotations

import atexit
import os
import time
from collections.abc import Callable
from typing import Any

SITEKEY = "0x4AAAAAADgxqF6QVMm0GLHH"
ORIGIN = "https://monochrome.tf"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
# Outer wall-clock ceiling on the whole Turnstile-solve dance. The in-page JS has
# its own 30s cap, but a wedged browser could block the evaluate() call itself
# forever; this monotonic deadline guarantees _ensure_jwt eventually returns.
_JWT_DEADLINE_S = 60.0
# Explicit per-evaluate timeout (ms). page.evaluate()/add_script_tag() do not
# accept a timeout kwarg, so this is enforced by the JS 'TIMEOUT' sentinel plus
# the outer _JWT_DEADLINE_S wrap rather than passed to Playwright.
_EVAL_TIMEOUT_MS = 30000

Clock = Callable[[], float]


class AmzSession:  # pragma: no cover - browser I/O
    """Lazily-started headed-Chromium session that resolves Amazon streams."""

    def __init__(
        self,
        amz_base: str = "https://amz.geeked.wtf",
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        self.amz_base = amz_base
        self._clock = clock
        self._pw: Any = None
        self._browser: Any = None
        self._page: Any = None
        self._jwt: str | None = None
        atexit.register(self.close)

    def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # In a container the headed browser runs under Xvfb and, as an unprivileged
        # user, needs --no-sandbox (Chrome's setuid sandbox can't init there). Gated
        # on an env flag so a local desktop install keeps the hardened default.
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if os.environ.get("CRATE_BROWSER_NO_SANDBOX"):
            launch_args += ["--no-sandbox", "--disable-dev-shm-usage"]
        self._browser = self._pw.chromium.launch(
            headless=False,
            channel="chromium",
            args=launch_args,
        )
        ctx = self._browser.new_context(user_agent=_UA)
        self._page = ctx.new_page()
        self._page.goto(ORIGIN, wait_until="domcontentloaded", timeout=45000)
        return self._page

    def _check_deadline(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise RuntimeError("Turnstile JWT not obtained within 60s")

    def _ensure_jwt(self) -> str:
        if self._jwt:
            return self._jwt
        deadline = self._clock() + _JWT_DEADLINE_S
        page = self._ensure_page()
        self._check_deadline(deadline)
        page.add_script_tag(
            url="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"
        )
        self._check_deadline(deadline)
        # The in-page render/solve is capped at _EVAL_TIMEOUT_MS via the JS
        # setTimeout sentinel; the surrounding _check_deadline calls bound the
        # blocking evaluate() itself against a wedged browser.
        token = page.evaluate(
            """async ([sk, timeoutMs]) => {
                await new Promise(r => { const i = setInterval(() => { if (window.turnstile) { clearInterval(i); r(); } }, 100); });
                return await new Promise(res => {
                    const d = document.createElement('div'); document.body.appendChild(d);
                    window.turnstile.render(d, { sitekey: sk, callback: res, 'error-callback': () => res('ERR') });
                    setTimeout(() => res('TIMEOUT'), timeoutMs);
                });
            }""",
            [SITEKEY, _EVAL_TIMEOUT_MS],
        )
        if not token or token in ("ERR", "TIMEOUT"):
            raise RuntimeError(f"Turnstile solve failed ({token}); needs a real headed browser")
        self._check_deadline(deadline)
        jwt = page.evaluate(
            """async ({ amz, tok }) => {
                const r = await fetch(amz + '/api/auth/turnstile', {
                    method: 'POST', headers: { 'content-type': 'application/json' },
                    body: JSON.stringify({ cf_turnstile_response: tok }),
                });
                return (await r.json()).access_token;
            }""",
            {"amz": self.amz_base, "tok": token},
        )
        if not jwt:
            raise RuntimeError("Turnstile JWT mint failed")
        self._check_deadline(deadline)
        self._jwt = str(jwt)
        return self._jwt

    def fetch_amz(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resolve an Amazon track (stream_url + decryption_key) from the page."""
        jwt = self._ensure_jwt()
        page = self._ensure_page()
        result = page.evaluate(
            """async ({ amz, jwt, params }) => {
                const u = amz + '/api/track/?' + new URLSearchParams(params).toString();
                const r = await fetch(u, { headers: { 'X-Turnstile-JWT': jwt, 'Origin': 'https://monochrome.tf' } });
                return { status: r.status, body: await r.json() };
            }""",
            {"amz": self.amz_base, "jwt": jwt, "params": {k: str(v) for k, v in params.items()}},
        )
        if result["status"] != 200:
            raise RuntimeError(f"amz/track failed: HTTP {result['status']}")
        return dict(result["body"])

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            finally:
                self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            finally:
                self._pw = None
