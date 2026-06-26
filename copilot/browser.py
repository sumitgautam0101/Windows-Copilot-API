"""Browser-backed sign-in and chat-token capture.

Playwright support for the pure-HTTP :class:`copilot.client.Copilot`: it does NOT
chat. Its sole job is to establish and refresh the signed-in session that the
HTTP driver runs on — interactive Microsoft/Google login plus headless capture of
the Copilot chat token.

``BrowserCopilot`` launches a **persistent** Playwright Chromium profile so that
Cloudflare clearance and any sign-in survive restarts. Two responsibilities:

  * :meth:`login` — opens a visible window for interactive sign-in, then warms up
    one chat turn to mint the token and snapshots ``session/token.json``.
  * :meth:`acquire_chat_token` — headless: returns the chat token, warming up a
    turn to mint/capture it when the MSAL cache can't be read directly.

Why a warm-up + WebSocket capture (not a localStorage read): federated *Google*
logins store the MSAL token cache **encrypted** and only mint the
``ChatAI.ReadWrite`` token on the first chat turn. So the token can't be read
from storage; instead we let the page open its own ``wss://.../c/api/chat``
socket and read ``accessToken`` (and ``X-UserIdentityType``) straight off that
URL — see :meth:`_install_ws_listener`. Microsoft logins expose a readable token
and skip the warm-up entirely.

All actual chatting lives in :mod:`copilot.driver` (pure HTTP). Recapture token
shapes with ``tests/diagnostic.py`` if Microsoft changes them.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright, Error as PlaywrightError

from .auth import DEFAULT_AUTH_FILE, DEFAULT_PROFILE_DIR
from .useragent import CHROME_UA

COPILOT_URL = "https://copilot.microsoft.com/"

# The Cloudflare Turnstile widget renders inside a cross-origin iframe served
# from challenges.cloudflare.com (page-load interstitial *and* the in-chat gate).
# We reach into that frame to click its checkbox — see _click_turnstile.
_TURNSTILE_IFRAME = "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile']"

# The one UA every browser context advertises — the same string the curl_cffi
# driver presents (see copilot/useragent.py). Applied to *both* headless and
# visible launches so clearance earned by either is reusable by the driver:
# cf_clearance is bound to the earning UA, so they must all match. It also hides
# the "HeadlessChrome/..." token headless Chromium otherwise leaks (a blatant bot
# tell). Because CHROME_UA tracks Playwright's bundled Chromium version, the
# override doesn't contradict the browser's native Sec-CH-UA client hint.
_STEALTH_UA = CHROME_UA

# Injected into every frame to hide the residual automation tell that survives
# --disable-blink-features=AutomationControlled in some Chromium builds.
_STEALTH_INIT_JS = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)

# --- in-page JavaScript -----------------------------------------------------

# Discover the Copilot chat MSAL access token from localStorage. The cache holds
# several tokens for different scopes; the chat WebSocket only accepts the one
# scoped 'ChatAI.ReadWrite' — a wrong-audience token (e.g. the Graph
# User.Read/Files.Read token) makes the WS upgrade 401. We therefore PREFER the
# ChatAI token and only fall back to the first token found if none matches.
# Returns null for anonymous sessions (anonymous chat may still work via cookies).
_FIND_TOKEN_JS = """
() => {
  try {
    let fallback = null;
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      const v = localStorage.getItem(k);
      if (v && v.indexOf('"credentialType":"AccessToken"') !== -1) {
        try {
          const o = JSON.parse(v);
          if (o && o.secret) {
            // Match the chat scope (e.g. '<resource>/ChatAI.ReadWrite'); take the
            // first non-matching token only as a last-resort fallback.
            if (o.target && o.target.indexOf('ChatAI') !== -1) return o.secret;
            if (!fallback) fallback = o.secret;
          }
        } catch (e) {}
      }
    }
    return fallback;
  } catch (e) {}
  return null;
}
"""

# True once the user is signed in, *before* the chat token is minted. MSAL writes
# an `msal.*.account.keys` index (a non-empty list of cached accounts) the moment
# sign-in completes — and, crucially, this index is NOT encrypted even when the
# token cache itself is, so it is a reliable sign-in signal for every account
# type (Microsoft *and* federated Google). We deliberately do not key off the
# ChatAI access token here: for Google logins MSAL stores the token cache
# *encrypted* ({id,nonce,data,...}) and only mints the chat token on the first
# chat turn, so waiting for it during login would never succeed (see login()).
_SIGNED_IN_JS = """
() => {
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.indexOf('account.keys') !== -1) {
        try {
          const a = JSON.parse(localStorage.getItem(k) || 'null');
          if (Array.isArray(a) ? a.length > 0 : (a && Object.keys(a).length > 0))
            return true;
        } catch (e) {}
      }
    }
  } catch (e) {}
  return false;
}
"""


class BrowserCopilot:
    """Drives Microsoft Copilot through a real Playwright browser.

    Parameters
    ----------
    profile_dir:
        Directory for the persistent Chromium profile (cookies, Cloudflare
        clearance, sign-in). Reused across runs.
    headless:
        Run without a visible window. Use ``False`` (or :meth:`login`) for the
        first interactive sign-in, then ``True`` afterwards.
    """

    label = "Microsoft Copilot (browser)"
    default_model = "Copilot"

    def __init__(
        self,
        profile_dir: str = DEFAULT_PROFILE_DIR,
        headless: bool = True,
        nav_timeout: int = 60,
        proxy: Optional[str] = None,
    ):
        self.profile_dir = str(Path(profile_dir).resolve())
        self.headless = headless
        self.nav_timeout = nav_timeout
        # Copilot consumer chat is geo-restricted. If you are outside a supported
        # region, route the browser through a proxy/VPN in a supported region,
        # e.g. proxy="http://user:pass@host:port" or "socks5://host:port".
        self.proxy = proxy

        self._pw = None
        self._context = None
        self._page = None
        self._login_log_fh = None
        # Chat token captured live off the page's own chat WebSocket. This is the
        # only way to recover the token for sessions whose MSAL cache is encrypted
        # (e.g. federated Google logins), where _FIND_TOKEN_JS cannot read it.
        self._captured_chat_token: Optional[str] = None
        self._captured_identity_type: Optional[str] = None
        self._ws_listener_installed = False
        # Set True once the page's chat socket streams a reply (an ``appendText``
        # frame). This is auto_clear's true success signal: a reply means the
        # browser turn passed the Cloudflare gate, so its cookies are worth
        # exporting — unlike the cf_clearance value, which often stays unchanged
        # when the browser replies using clearance it already holds.
        self._warmup_replied = False

    # -- lifecycle ----------------------------------------------------------

    def start(self, headless: Optional[bool] = None) -> "BrowserCopilot":
        """Launch the persistent browser context and open Copilot."""
        if self._context is not None:
            return self
        if headless is not None:
            self.headless = headless
        try:
            self._pw = sync_playwright().start()
            launch_kwargs = dict(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
                # Drop the "Chrome is being controlled by automated software"
                # switch; its presence is a cheap bot tell Turnstile reads.
                ignore_default_args=["--enable-automation"],
            )
            # Pin the UA on every launch, headless or visible. Both must earn
            # cf_clearance under the exact string the curl_cffi driver replays;
            # leaving the visible window on Playwright's native UA would bind the
            # clearance to a version the driver doesn't present, re-gating chat.
            launch_kwargs["user_agent"] = _STEALTH_UA
            if self.proxy:
                launch_kwargs["proxy"] = self._parse_proxy(self.proxy)
            self._context = self._pw.chromium.launch_persistent_context(
                self.profile_dir,
                **launch_kwargs,
            )
            # Mask the residual navigator.webdriver flag for every frame, before
            # any page script (incl. Turnstile's) runs.
            try:
                self._context.add_init_script(_STEALTH_INIT_JS)
            except PlaywrightError:
                pass
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.set_default_timeout(self.nav_timeout * 1000)
            self._page.goto(COPILOT_URL, wait_until="domcontentloaded")
            # Give Cloudflare a moment to clear on first paint. We deliberately do
            # NOT wait for "networkidle": Copilot's SPA keeps telemetry/heartbeat
            # connections open indefinitely, so the network never goes idle and the
            # wait would always time out. A short fixed settle is enough.
            self._page.wait_for_timeout(2000)
        except PlaywrightError as exc:
            self.close()
            raise ConnectionError(f"Failed to start browser: {exc}") from exc
        return self

    @staticmethod
    def _parse_proxy(proxy: str) -> dict:
        """Turn a ``scheme://user:pass@host:port`` string into Playwright form."""
        from urllib.parse import urlparse

        u = urlparse(proxy)
        server = f"{u.scheme}://{u.hostname}:{u.port}" if u.port else f"{u.scheme}://{u.hostname}"
        cfg = {"server": server}
        if u.username:
            cfg["username"] = u.username
        if u.password:
            cfg["password"] = u.password
        return cfg

    def region_blocked(self) -> bool:
        """True if Copilot is showing the 'Not available in your region' notice."""
        if self._page is None:
            return False
        try:
            text = self._page.evaluate("() => document.body ? document.body.innerText : ''")
        except PlaywrightError:
            return False
        return "available in your region" in (text or "").lower()

    def close(self) -> None:
        for attr, closer in (
            ("_context", lambda c: c.close()),
            ("_pw", lambda p: p.stop()),
            ("_login_log_fh", lambda f: f.close()),
        ):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    closer(obj)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._page = None

    def __enter__(self) -> "BrowserCopilot":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- auth ---------------------------------------------------------------

    def login(self, path: str = DEFAULT_AUTH_FILE, timeout: int = 300) -> dict:
        """Open a visible window for interactive Microsoft/Google sign-in.

        Auto-detects success — a cached account appearing in the page (the moment
        sign-in completes, see :data:`_SIGNED_IN_JS`) — then **warms up** the
        session with one throwaway chat turn to mint the Copilot chat token and
        captures it off the page's own chat WebSocket. This warm-up is what makes
        federated *Google* logins work: their MSAL cache is encrypted and the chat
        token is only minted on the first turn, so the old "wait for the token in
        localStorage" approach timed out (~5 min) and saved a null token.
        Microsoft accounts already have a readable token, so the warm-up returns
        instantly and their flow is unchanged.

        No key-press needed; the browser closes itself. Every step is appended to
        ``<session>/login.log``. ``timeout`` bounds the wait. The session persists
        in ``profile_dir`` for headless reuse.
        """
        self.close()
        self.start(headless=False)
        self._install_ws_listener()

        log = self._open_login_log(Path(path).resolve().parent / "login.log")
        log(f"login started; browser open at {COPILOT_URL}")
        self._mirror_page_events(log)

        print(
            "\nA browser window is open at copilot.microsoft.com.\n"
            "Sign in (and pass any 'verify you're human' check).\n"
            "It finishes by itself once sign-in is detected — no need to press Enter.\n"
        )

        # Wait for sign-in (a cached account), not for the chat token: the token
        # may not exist until the first turn. Bail early on window close/timeout.
        detected = False
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._window_closed():
                log("browser window closed before sign-in was detected")
                break
            if self.signed_in():
                log("sign-in detected (account cached)")
                detected = True
                break
            try:
                self._page.wait_for_timeout(1500)
            except PlaywrightError:
                break

        token = None
        before = self._clearance_value()
        if detected:
            print("Signed in — finishing setup (warm-up + Cloudflare clearance)...")
            log("warming up to mint the chat token and earn cf_clearance")
            try:
                # A single warm-up turn does double duty: the page opens its chat
                # socket (the WS listener reads the token off its URL) and, by
                # passing the in-chat Cloudflare gate, earns the cf_clearance the
                # pure-HTTP driver reuses — so the first `ask` after login needs no
                # second browser. We click any Turnstile and wait for the reply.
                self._warmup_replied = False
                if self._send_warmup():
                    self._await_gate_pass(
                        before, timeout=max(30, int(deadline - time.time()))
                    )
                token = self.access_token()
            except PlaywrightError as exc:
                log(f"warm-up error: {exc}")
            cleared = self._clearance_value() != before or self._warmup_replied
            log(f"chat token captured: {'yes' if token else 'no'}"
                f" (identity={self._captured_identity_type});"
                f" clearance earned: {'yes' if cleared else 'no'}")
            print("Cloudflare clearance earned." if cleared
                  else "Note: clearance not confirmed; first request may open a browser.")
        else:
            log(f"not signed in within {timeout}s; snapshotting current state")
            print("Sign-in not detected; saving whatever session state exists.")

        # Snapshot for the headless curl_cffi path.
        auth: dict = {}
        try:
            auth = self.export_auth(path=path, stamp=time.time())
            log(f"auth snapshot saved to {path} (access_token={'yes' if auth.get('access_token') else 'no'}"
                f", identity={auth.get('identity_type')})")
            print(f"Auth snapshot saved to {path}")
        except Exception as exc:
            log(f"could not snapshot auth: {exc}")
            print(f"(could not snapshot auth: {exc})")

        log("closing browser")
        self.close()
        print(f"Session saved to {self.profile_dir}")
        return auth

    def _open_login_log(self, log_path: Path):
        """Return a best-effort timestamped append-logger to ``log_path``.

        The handle is parked on the context so :meth:`close` can release it; if the
        file can't be opened, the returned logger is a silent no-op.
        """
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._login_log_fh = log_path.open("a", encoding="utf-8")
        except OSError:
            self._login_log_fh = None

        def log(message: str) -> None:
            fh = self._login_log_fh
            if fh is None:
                return
            try:
                fh.write(f"{datetime.now(timezone.utc).isoformat()}\t{message}\n")
                fh.flush()
            except Exception:
                pass

        return log

    def _mirror_page_events(self, log) -> None:
        """Stream main-frame navigations and console errors into the login log."""
        try:
            self._page.on(
                "framenavigated",
                lambda fr: fr == self._page.main_frame and log(f"navigated: {fr.url}"),
            )
            self._page.on(
                "console",
                lambda m: m.type == "error" and log(f"console.error: {m.text}"),
            )
        except PlaywrightError:
            pass

    def _window_closed(self) -> bool:
        """True if the page/context is gone (e.g. the user closed the window)."""
        try:
            return self._page is None or self._page.is_closed()
        except Exception:
            return True

    def access_token(self) -> Optional[str]:
        """Return the Copilot chat token, or ``None`` if not available.

        Prefers a token captured live off the page's own chat WebSocket (the only
        source that works when the MSAL cache is encrypted, e.g. Google logins),
        and otherwise falls back to reading the unencrypted MSAL cache via
        ``_FIND_TOKEN_JS`` (Microsoft logins). Call :meth:`acquire_chat_token`
        first to ensure one of these is populated.
        """
        if self._captured_chat_token:
            return self._captured_chat_token
        self._ensure_started()
        try:
            return self._page.evaluate(_FIND_TOKEN_JS)
        except PlaywrightError:
            return None

    def signed_in(self) -> bool:
        """True once a Microsoft/Google account is cached (sign-in complete)."""
        self._ensure_started()
        try:
            return bool(self._page.evaluate(_SIGNED_IN_JS))
        except PlaywrightError:
            return False

    def _install_ws_listener(self) -> None:
        """Capture the chat token off the page's own chat WebSocket.

        The page opens ``wss://.../c/api/chat?...&accessToken=<token>`` (plus, for
        federated logins, ``&X-UserIdentityType=google``) when it sends a turn.
        Reading the token here is encryption-proof: the page has already decrypted
        it. parse_qs URL-decodes the value, so we store the raw token (the drivers
        re-quote it when building their own socket URL)."""
        if self._ws_listener_installed or self._page is None:
            return

        def on_ws(ws):
            try:
                url = ws.url
                if "/c/api/chat" not in url:
                    return
                if "accessToken=" in url:
                    q = parse_qs(urlparse(url).query)
                    tok = (q.get("accessToken") or [None])[0]
                    if tok:
                        self._captured_chat_token = tok
                        self._captured_identity_type = (q.get("X-UserIdentityType") or [None])[0]
                # Watch reply frames so auto_clear knows the turn passed the gate.
                ws.on("framereceived", self._on_chat_frame)
            except Exception:
                pass

        try:
            self._page.on("websocket", on_ws)
            self._ws_listener_installed = True
        except PlaywrightError:
            pass

    def _on_chat_frame(self, payload) -> None:
        """Flag a passed turn when the chat socket streams reply content.

        An ``appendText`` (or ``imageGenerated``) frame means the warm-up reply is
        flowing, i.e. Cloudflare let the turn through — auto_clear's success
        signal. A ``challenge`` frame contains neither, so this never false-fires
        on the gate itself."""
        try:
            data = payload if isinstance(payload, str) else bytes(payload).decode("utf-8", "ignore")
        except Exception:
            return
        if "appendText" in data or "imageGenerated" in data:
            self._warmup_replied = True

    def _send_warmup(self, text: str = "hi") -> bool:
        """Send one message through the page composer to mint the chat token.

        Returns True if a send was attempted. Federated (Google) sessions only
        mint the ChatAI token on the first chat turn, so we trigger one here and
        let :meth:`_install_ws_listener` capture the token off the resulting
        socket."""
        for sel in ("textarea", "div[contenteditable='true']", "[role='textbox']"):
            try:
                self._page.wait_for_selector(sel, state="visible", timeout=8000)
            except PlaywrightError:
                continue
            try:
                self._page.click(sel)
                self._page.keyboard.type(text, delay=15)
                self._page.keyboard.press("Enter")
                return True
            except PlaywrightError:
                continue
        return False

    def acquire_chat_token(
        self, timeout: int = 60, warmup: bool = True, signin_grace: int = 8
    ) -> Optional[str]:
        """Return a usable chat token, minting it via a warm-up turn if needed.

        Fast path: a token already readable (captured, or unencrypted MSAL cache)
        is returned immediately — this is the common Microsoft case. Otherwise, if
        ``warmup`` and the user is signed in, send one throwaway message and
        capture the token off the chat WebSocket (the encrypted-cache / Google
        case). Returns ``None`` if no token could be obtained within ``timeout``.

        ``signin_grace`` bounds how long we wait for an *existing* sign-in to
        register before giving up. A headless refresh can't perform interactive
        sign-in, so on a not-signed-in profile we bail after this short grace
        instead of blocking the full ``timeout`` — that wait is what made the
        no-session path feel hung before it fell through to a visible login.
        Sign-in normally registers within ~1-2s of page load (an already-signed-in
        profile passes the grace immediately).
        """
        self._ensure_started()
        self._install_ws_listener()

        tok = self.access_token()
        if tok or not warmup:
            return tok

        deadline = time.time() + timeout
        signin_deadline = time.time() + min(signin_grace, timeout)
        while time.time() < signin_deadline and not self.signed_in():
            if self._window_closed():
                return None
            self._page.wait_for_timeout(500)
        if not self.signed_in():
            return None

        if not self._send_warmup():
            return self.access_token()

        while time.time() < deadline:
            if self._captured_chat_token:
                return self._captured_chat_token
            if self._window_closed():
                break
            self._page.wait_for_timeout(500)
        return self.access_token()

    @staticmethod
    def _clear_log(msg: str) -> None:
        """Emit an ``auto_clear`` progress line to stderr (keeps stdout clean)."""
        print(f"[copilot] clearance: {msg}", file=sys.stderr, flush=True)

    def _clearance_value(self) -> Optional[str]:
        """Return the current ``cf_clearance`` cookie value, or ``None``.

        Cloudflare mints a *new* ``cf_clearance`` when a challenge is solved, so a
        change in this value is the reliable signal that fresh clearance was
        actually earned (:meth:`auto_clear` waits on it). The cookie is set on the
        ``.copilot.microsoft.com`` domain."""
        if self._context is None:
            return None
        try:
            for c in self._context.cookies():
                if c.get("name") == "cf_clearance":
                    return c.get("value")
        except PlaywrightError:
            pass
        return None

    def _click_turnstile(self, timeout_ms: int = 4000) -> bool:
        """Best-effort: click the Cloudflare Turnstile checkbox if one is showing.

        Returns True if a checkbox was clicked. The widget lives in a cross-origin
        Cloudflare iframe whose checkbox can sit behind nested iframes / shadow
        roots, so we try three escalating strategies (the recursive-finder idea
        from DrissionPage-based bypassers, adapted to Playwright):

          1. Scan *all* frames — ``page.frames`` is flat and includes frames nested
             inside shadow roots that a top-level CSS ``frame_locator`` can't
             reach — for the Cloudflare challenge frame, and click its checkbox.
             Playwright locators pierce open shadow roots inside that frame for us.
          2. Fall back to the top-level ``frame_locator`` selector.
          3. Last resort: click the iframe host element at the checkbox offset
             (left-of-centre), where the real checkbox sits.

        A click only *passes* when Cloudflare already trusts this browser; on a
        low-trust session (datacenter/VPN IP) it can escalate to a puzzle a click
        can't solve — :meth:`auto_clear`'s caller detects that (the turn never
        replies) and falls back to a visible browser for a human.
        """
        if self._page is None:
            return False
        deadline = time.time() + timeout_ms / 1000
        while True:
            # 1. flat-frame scan (robust to shadow-root / nested-iframe nesting)
            frame = self._find_turnstile_frame()
            if frame is not None and self._click_in_frame(frame):
                return True
            # 2. top-level frame_locator, then 3. offset click on the host iframe
            try:
                if self._page.query_selector(_TURNSTILE_IFRAME) is not None:
                    fl = self._page.frame_locator(_TURNSTILE_IFRAME).first
                    for sel in ("input[type='checkbox']", "label"):
                        try:
                            fl.locator(sel).first.click(timeout=1500)
                            return True
                        except PlaywrightError:
                            continue
                    if self._click_turnstile_by_offset():
                        return True
            except PlaywrightError:
                pass
            if time.time() >= deadline:
                return False
            self._page.wait_for_timeout(300)

    def _find_turnstile_frame(self):
        """Return the Cloudflare challenge frame among all frames, or ``None``.

        ``page.frames`` is a flat list of every frame in the page — including ones
        embedded inside shadow roots — so it finds the Turnstile iframe even when a
        top-level CSS selector can't reach it. This is the Playwright equivalent of
        the recursive shadow-root/iframe descent the DrissionPage bypassers do."""
        if self._page is None:
            return None
        try:
            for fr in self._page.frames:
                u = (fr.url or "").lower()
                if "challenges.cloudflare.com" in u or "turnstile" in u:
                    return fr
        except PlaywrightError:
            pass
        return None

    @staticmethod
    def _click_in_frame(frame) -> bool:
        """Click the Turnstile checkbox inside an already-resolved challenge frame."""
        for sel in ("input[type='checkbox']", "label", "body"):
            try:
                frame.locator(sel).first.click(timeout=1500)
                return True
            except PlaywrightError:
                continue
        return False

    def _click_turnstile_by_offset(self) -> bool:
        """Click the Turnstile iframe host where the checkbox sits (left-of-centre).

        A coordinate click on the host element, used when the checkbox inside the
        frame can't be targeted directly (cross-origin isolation / odd markup)."""
        try:
            host = self._page.query_selector(_TURNSTILE_IFRAME)
            if host is None:
                return False
            box = host.bounding_box()
            if not box or box.get("width", 0) < 1:
                return False
            x = box["x"] + min(30, box["width"] / 2)
            y = box["y"] + box["height"] / 2
            self._page.mouse.click(x, y)
            return True
        except PlaywrightError:
            return False

    def _await_gate_pass(self, before_clearance: Optional[str], timeout: int = 60) -> bool:
        """Wait for an already-sent warm-up turn to pass the Cloudflare gate.

        Clicks any Turnstile checkbox that appears and returns once the turn
        streams a reply (``appendText`` -> gate passed) or a fresh ``cf_clearance``
        is issued, or ``timeout`` elapses. Assumes the caller already installed the
        WS listener, reset ``_warmup_replied``, and sent the warm-up. Shared by
        :meth:`auto_clear` and :meth:`login` so one warm-up both mints the token
        and earns clearance. Returns whether the gate was passed."""
        deadline = time.time() + timeout
        clicked = False
        while time.time() < deadline:
            if self._window_closed():
                self._clear_log("browser window was closed")
                break
            if self._click_turnstile(timeout_ms=1000) and not clicked:
                clicked = True
                self._clear_log("clicked the in-chat Turnstile checkbox")
            # Success = the turn replied (passed the gate). A changed cf_clearance
            # is a secondary signal for the rare case where the cookie refreshes
            # but no reply frame is seen.
            if self._warmup_replied:
                self._clear_log("warm-up reply received — gate passed")
                break
            current = self._clearance_value()
            if current and current != before_clearance:
                self._clear_log("fresh cf_clearance issued — gate passed")
                break
            self._page.wait_for_timeout(500)
        else:
            self._clear_log(f"turn did not pass the gate within {timeout}s")
        if not self._window_closed():
            self._page.wait_for_timeout(1000)  # let the cookie settle to disk
        return self._warmup_replied or (self._clearance_value() != before_clearance)

    def auto_clear(
        self, path: str = DEFAULT_AUTH_FILE, warmup: bool = True, timeout: int = 60
    ) -> bool:
        """Refresh Cloudflare clearance for the pure-HTTP driver, then snapshot it.

        Loads Copilot and clicks any Turnstile checkbox that appears — on page
        load and, when ``warmup`` and signed in, after sending one throwaway chat
        turn (the in-chat Turnstile is the gate observed on the chat socket). Then
        snapshots the refreshed cookies + token to ``path`` so the curl_cffi
        driver can reuse the earned ``cf_clearance``.

        Headless when constructed with ``headless=True`` (the default): a fully
        automatic solve whenever Cloudflare trusts the session. When Cloudflare
        escalates to an interactive puzzle (low-trust egress, e.g. a VPN), the
        headless click won't pass — construct with ``headless=False`` so a human
        can finish it. Returns True if a snapshot with cookies was written; the
        caller verifies *real* success by retrying the chat turn (a snapshot can
        be written even when clearance didn't actually pass).
        """
        self._ensure_started()
        self._install_ws_listener()
        mode = "headless" if self.headless else "visible"
        self._clear_log(f"loaded Copilot ({mode}); checking Cloudflare clearance")

        # Remember the pre-existing clearance so we can tell when a *fresh* one is
        # earned. The driver only calls us because the current cf_clearance is
        # stale, so success = this value changing (or appearing), not merely being
        # present. We deliberately do NOT key off the captured chat token: the page
        # opens its chat WebSocket (and we capture the token off its URL) *before*
        # the Turnstile challenge frame arrives, so that signal fires too early and
        # used to close the browser before the checkbox even appeared.
        before = self._clearance_value()

        # 1. Solve any challenge gating the page itself on load.
        if self._click_turnstile():
            self._clear_log("clicked a page-load Turnstile checkbox")

        # 2. Trigger the in-chat Turnstile (the gate seen on the chat socket) with
        #    one throwaway turn, then wait for clearance to actually refresh —
        #    clicking any checkbox that appears (headless auto-solve) or letting a
        #    human click it (visible window). Sending one turn is what the manual
        #    diagnostic does to earn clearance.
        if warmup and self.signed_in():
            self._warmup_replied = False
            self._clear_log("sending a warm-up turn to trigger the in-chat challenge")
            self._send_warmup()
            self._clear_log(f"waiting up to {timeout}s for the turn to pass the gate"
                            + ("" if self.headless else " (click the checkbox if shown)"))
            self._await_gate_pass(before, timeout=timeout)
        elif warmup:
            self._clear_log("not signed in — skipping warm-up; snapshotting state")

        auth = self.export_auth(path=path, stamp=time.time())
        # Report whether the turn actually passed the gate (reply seen or fresh
        # clearance), not just that a snapshot was written; the client uses this to
        # decide whether to escalate to a visible browser.
        earned = bool(auth.get("cookies")) and (
            self._warmup_replied or self._clearance_value() != before
        )
        self._clear_log("done — clearance refreshed" if earned
                        else "done — no clearance earned")
        return earned

    def cookies(self) -> Dict[str, str]:
        """Return the signed-in Microsoft cookies as a name->value dict."""
        self._ensure_started()
        try:
            raw = self._context.cookies()
        except PlaywrightError:
            return {}
        return {c["name"]: c["value"] for c in raw if "microsoft.com" in c.get("domain", "")}

    def export_auth(self, path: str = DEFAULT_AUTH_FILE, stamp: Optional[float] = None) -> dict:
        """Snapshot the signed-in cookies + access token to ``path`` as JSON.

        ``stamp`` is the epoch seconds to record as ``saved_at`` (pass
        ``time.time()`` from the caller). Returns the auth dict.
        """
        auth = {
            "cookies": self.cookies(),
            "access_token": self.access_token(),
            # Federated logins (Google) ride an extra &X-UserIdentityType= on the
            # chat socket; the drivers replay it. None for Microsoft accounts.
            "identity_type": self._captured_identity_type,
            "saved_at": stamp if stamp is not None else 0,
        }
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(auth, indent=2), encoding="utf-8")
        return auth

    # -- internals ----------------------------------------------------------

    def _ensure_started(self) -> None:
        if self._context is None or self._page is None:
            self.start()
