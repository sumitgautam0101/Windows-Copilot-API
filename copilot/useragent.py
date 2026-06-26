"""The single User-Agent the whole bridge presents to Cloudflare.

Cloudflare binds ``cf_clearance`` to the *exact* User-Agent string that earned
it. The bridge touches that one cookie from three places — the curl_cffi chat
driver (which *uses* it), the headless refresh, and the interactive login (which
*earn* it) — so all three must present a byte-identical UA or the clearance is
distrusted and the chat socket gates every turn behind a Cloudflare Turnstile.

Keeping the string here (imported by both :mod:`copilot.driver` and
:mod:`copilot.browser`) makes drift impossible.

Why these exact values:

* ``CHROME_UA`` is a real desktop **Windows** Chrome UA. We standardise on the
  same major version Playwright actually bundles (see the maintenance note), so
  overriding a launched Chromium's UA to this string does *not* contradict the
  browser's native ``Sec-CH-UA`` client hint — both say the same version.
* ``IMPERSONATE_TARGET`` pins curl_cffi to a fixed TLS/HTTP2 fingerprint. Left as
  the bare ``"chrome"`` alias it tracks curl_cffi's ``DEFAULT_CHROME``, which
  advances on every upgrade (and ships a *macOS* UA) — a moving target that
  silently re-breaks the UA match. Pin to the closest stable profile instead; the
  driver overrides the UA + client hints on top so the wire presentation stays
  Windows/``CHROME_UA`` regardless of the profile's native UA.

MAINTENANCE: bump ``CHROME_UA``'s major version whenever ``playwright install``
upgrades the bundled Chromium (check ``chromium.launch().version``). If the
constant lags the real browser, the browser's native ``Sec-CH-UA`` out-drifts the
spoofed UA and Turnstile sees the mismatch. One line, one place.
"""

# Real desktop Windows Chrome. Must match Playwright's bundled Chromium major
# version (currently 148) so the UA override introduces no client-hint conflict.
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# Client hints that must accompany CHROME_UA so the platform/version a server
# reads from the hints agrees with the UA line. Used by the curl_cffi driver,
# which otherwise emits the impersonation profile's native (macOS) hints.
CHROME_CLIENT_HINTS = {
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua": '"Google Chrome";v="148", "Chromium";v="148", "Not_A Brand";v="24"',
}

# Pinned curl_cffi impersonation profile (TLS/HTTP2 fingerprint). Closest stable
# profile to CHROME_UA's version; the UA itself is overridden on top.
IMPERSONATE_TARGET = "chrome146"
