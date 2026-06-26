"""Pure-HTTP Copilot driver.

Speaks Microsoft Copilot's consumer chat protocol directly over a
Cloudflare-impersonating ``curl_cffi`` session — no browser required. This is the
low-level engine; most callers should use :class:`copilot.client.CopilotClient`.
See :mod:`copilot.browser` for the Playwright-backed fallback.
"""

import json
import time
import uuid
from select import select
from typing import Dict, Optional
from urllib.parse import quote

from curl_cffi.const import CurlECode, CurlInfo
from curl_cffi.curl import CurlError
from curl_cffi.requests import Session, CurlWsFlag

# curl_cffi's WebSocket.recv() loops on CURLE_AGAIN forever (select() then retry)
# and never returns on an idle socket, so we drive the fragment loop ourselves to
# honour a deadline. CURL_SOCKET_BAD is libcurl's "no active socket" sentinel.
_CURL_SOCKET_BAD = -1

from .challenges import solve_copilot_challenge, solve_hashcash
from .models import AbstractProvider, Conversation, ImageResponse, ImageType
from .protocol import CHAT_WEBSOCKET_URL, CONSENTS_FRAME, SET_OPTIONS_FRAME
from .useragent import CHROME_CLIENT_HINTS, CHROME_UA, IMPERSONATE_TARGET
from .utils import drain_json, is_accepted_format, raise_for_status, to_bytes


class ClearanceRequired(RuntimeError):
    """The chat socket demanded a Cloudflare Turnstile token we can't mint here.

    Copilot gates a turn behind a ``challenge`` frame with ``method`` either
    ``null`` or ``"cloudflare"`` whenever the session's ``cf_clearance`` cookie is
    stale or missing (confirmed by capturing the real web client: it answers a
    ``{method:null}`` frame with a ``method:"cloudflare"`` Turnstile token). A
    Turnstile token can only be produced by executing Cloudflare's challenge JS in
    a real browser, so the pure-HTTP driver can't satisfy it. The caller should
    refresh clearance in a browser (see
    :meth:`copilot.browser.BrowserCopilot.auto_clear`) and retry the turn.
    """


class Copilot(AbstractProvider):
    label = "Microsoft Copilot"
    url = "https://copilot.microsoft.com"
    working = True
    supports_stream = True
    default_model = "Copilot"
    needs_auth = False  # consumer chat works anonymously (cookies only)
    websocket_url = CHAT_WEBSOCKET_URL
    conversation_url = f"{url}/c/api/conversations"

    def create_completion(
            self,
            prompt: str,
            stream: bool = False,
            proxy: str = None,
            timeout: int = 900,
            image: ImageType = None,
            conversation: Optional[Conversation] = None,
            conversation_id: str = None,
            return_conversation: bool = False,
            cookies: Dict[str, str] = None,
            access_token: str = None,
            identity_type: str = None,
            **kwargs
        ):
        """Stream a Copilot reply to ``prompt``.

        Runs Copilot's own chat protocol over a Cloudflare-impersonating
        ``curl_cffi`` session: ``POST /c/api/conversations`` then a chat
        WebSocket (``send`` -> proof-of-work ``challenge`` -> ``appendText``* ->
        ``done``). The challenge is solved in-process (see
        :mod:`copilot.challenges`); no browser is required.

        ``prompt`` is the user message sent straight to the chat socket (the
        protocol has no separate system/role channel). Anonymous by default;
        pass ``cookies`` and/or ``access_token`` (e.g. exported from a signed-in
        browser session) to run as a logged-in user — required where anonymous
        consumer chat is region-restricted.

        Conversation targeting (first match wins):
          * ``conversation`` — reuse an existing :class:`Conversation` object;
          * ``conversation_id`` — resume a conversation by its id string (no
            create call), e.g. one saved from a previous run;
          * neither — create a fresh conversation. With ``return_conversation``
            the new :class:`Conversation` is yielded first.
        """
        # Resolve auth: explicit args win, else fall back to the conversation's.
        if cookies is None and conversation is not None:
            cookies = conversation.cookies
        if access_token is None and conversation is not None:
            access_token = conversation.access_token

        # Auth model mirrors the browser:
        #   * REST calls (conversation create, attachment upload) authenticate by
        #     COOKIE only. Sending the token as an Authorization: Bearer header
        #     there gets a 401 (browsers never do it), so we don't.
        #   * the chat WebSocket carries the signed-in identity via its
        #     ?accessToken= param. This must be the Copilot chat token (MSAL scope
        #     ChatAI.ReadWrite, selected in browser._FIND_TOKEN_JS): a
        #     wrong-audience token 401s the WS upgrade, while *no* token makes the
        #     chat backend treat the session as anonymous -> chat-service-
        #     unavailable in geo-restricted regions (e.g. India).
        # Mirror the real client's URL shape: api-version, then a fresh
        # per-connection clientSessionId, then the access token. The current chat
        # backend expects clientSessionId; omitting it is one trigger for an
        # `invalid-event` rejection.
        websocket_url = f"{self.websocket_url}&clientSessionId={uuid.uuid4()}"
        if access_token:
            websocket_url = f"{websocket_url}&accessToken={quote(access_token)}"
            # Federated (Google) tokens ride an extra identity-type marker on the
            # real client's socket; replay it so the upgrade isn't rejected.
            if identity_type:
                websocket_url = f"{websocket_url}&X-UserIdentityType={quote(identity_type)}"

        with Session(
            timeout=timeout,
            proxy=proxy,
            # Pin the TLS/HTTP2 fingerprint, then override the UA + client hints so
            # the wire presentation is a fixed Windows Chrome. cf_clearance is bound
            # to the earning UA; the browsers that earn it present this same string,
            # so the driver must too — otherwise every turn is gated behind a
            # Cloudflare Turnstile. See copilot/useragent.py.
            impersonate=IMPERSONATE_TARGET,
            headers={"User-Agent": CHROME_UA, **CHROME_CLIENT_HINTS},
            cookies=cookies,
        ) as session:
            # Establish cookies + Cloudflare clearance (anonymous is fine).
            session.get(f"{self.url}/")

            if conversation is not None:
                conversation_id = conversation.conversation_id
            elif conversation_id is not None:
                pass  # resume an existing conversation by id; skip create
            else:
                response = session.post(self.conversation_url)
                raise_for_status(response)
                conversation_id = response.json().get("id")
                if return_conversation:
                    yield Conversation(conversation_id, session.cookies.jar)

            images = []
            if image is not None:
                data = to_bytes(image)
                response = session.post(
                    f"{self.url}/c/api/attachments",
                    headers={"content-type": is_accepted_format(data)},
                    data=data,
                )
                raise_for_status(response)
                images.append({"type": "image", "url": response.json().get("url")})

            send_frame = json.dumps({
                "event": "send",
                "conversationId": conversation_id,
                "content": [*images, {"type": "text", "text": prompt}],
                "mode": "smart",
                "context": {},
            }).encode()

            wss = session.ws_connect(websocket_url)
            # Initialise the session before sending: setOptions then
            # reportLocalConsents. A `send` issued first is rejected with
            # `invalid-event` (see the handshake constants above).
            wss.send(json.dumps(SET_OPTIONS_FRAME).encode(), CurlWsFlag.TEXT)
            wss.send(json.dumps(CONSENTS_FRAME).encode(), CurlWsFlag.TEXT)
            wss.send(send_frame, CurlWsFlag.TEXT)
            yield from self._read_stream(wss, send_frame, timeout)

    def _read_stream(self, wss, send_frame: bytes, timeout: int, idle_timeout: int = 60):
        """Consume chat-socket frames, solving challenges, yielding text/images.

        ``idle_timeout`` bounds how long we wait for the *next* frame: the chat
        backend normally answers within a second, so prolonged silence means a
        stalled socket (or a challenge we failed to answer) — we raise rather
        than block for the full ``timeout``.
        """
        buffer = b""
        is_started = False
        answered = False
        image_prompt = None
        last_msg = None

        overall_deadline = time.time() + timeout
        while True:
            idle_deadline = time.time() + idle_timeout
            try:
                chunk = self._recv_frame(wss, min(overall_deadline, idle_deadline))
            except Exception:
                break  # socket closed/errored -> end of stream
            if chunk is None:  # deadline passed with no frame
                if time.time() >= overall_deadline:
                    raise TimeoutError(f"Copilot stream exceeded {timeout}s")
                raise TimeoutError(
                    f"Copilot chat socket went silent for {idle_timeout}s; "
                    f"last frame was {last_msg!r}."
                )

            buffer += chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode()
            messages, buffer = drain_json(buffer)
            for msg in messages:
                last_msg = msg
                event = msg.get("event")
                if event == "challenge":
                    method = msg.get("method")
                    # A Cloudflare Turnstile (method null/"cloudflare") can arrive
                    # at any point — including *after* a proof-of-work challenge was
                    # already answered this turn — and we can never mint its token
                    # here. Surface it regardless of ``answered`` so a stale
                    # cf_clearance becomes a clean ClearanceRequired instead of a
                    # silent 60s idle timeout (the frame would otherwise be ignored).
                    if method in (None, "cloudflare"):
                        raise ClearanceRequired(
                            "Copilot chat is gated behind a Cloudflare Turnstile "
                            f"(challenge method={method!r}); cf_clearance is stale "
                            "or missing. Refresh clearance in a browser "
                            "(copilot.browser.BrowserCopilot.auto_clear) and retry."
                        )
                    if answered:
                        continue  # already answered the PoW for this turn; ignore echo
                    token = self._solve_challenge(msg)
                    if token is None:
                        raise RuntimeError(
                            f"Unsolvable Copilot challenge (method={method!r}). "
                            "Microsoft may have escalated to a browser-only challenge; "
                            "fall back to copilot.browser.BrowserCopilot."
                        )
                    wss.send(json.dumps({
                        "event": "challengeResponse",
                        "token": token,
                        "method": msg.get("method"),
                        "id": msg.get("id"),
                    }).encode(), CurlWsFlag.TEXT)
                    answered = True
                    # The client re-sends the held message after a challenge.
                    wss.send(send_frame, CurlWsFlag.TEXT)
                elif event == "appendText":
                    is_started = True
                    yield msg.get("text")
                elif event == "generatingImage":
                    image_prompt = msg.get("prompt")
                elif event == "imageGenerated":
                    yield ImageResponse(msg.get("url"), image_prompt, {"preview": msg.get("thumbnailUrl")})
                elif event == "done":
                    return
                elif event == "error":
                    code = msg.get("errorCode") or msg
                    if code == "chat-service-unavailable":
                        raise RuntimeError(
                            "Copilot error: chat-service-unavailable. The chat backend is "
                            "typically geo-restricted; if you are outside a supported region, "
                            "retry via a proxy in a supported region, e.g. "
                            "create_completion(..., proxy='http://user:pass@host:port')."
                        )
                    raise RuntimeError(f"Copilot error: {code}")

        if not is_started:
            raise RuntimeError(f"Invalid response: {last_msg}")

    @staticmethod
    def _recv_frame(wss, deadline: float):
        """Block for one complete WS frame, or return ``None`` past ``deadline``.

        Reassembles libcurl's fragments like ``curl_cffi``'s own ``recv()`` but
        breaks out of the ``CURLE_AGAIN`` wait once ``deadline`` (epoch seconds)
        is reached, so an idle socket can't hang us indefinitely. Non-AGAIN curl
        errors (e.g. a closed connection) propagate to the caller.
        """
        sock_fd = wss.curl.getinfo(CurlInfo.ACTIVESOCKET)
        if sock_fd == _CURL_SOCKET_BAD:
            raise ConnectionError("WebSocket has no active socket")
        chunks = []
        while True:
            try:
                chunk, frame = wss.recv_fragment()
                chunks.append(chunk)
                if frame.bytesleft == 0 and frame.flags & CurlWsFlag.CONT == 0:
                    return b"".join(chunks)
            except CurlError as e:
                if e.code != CurlECode.AGAIN:
                    raise
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                select([sock_fd], [], [], min(0.5, remaining))

    @staticmethod
    def _solve_challenge(msg: dict):
        """Return the challenge-response token, or ``None`` if we can't solve it.

        Copilot's chat socket precedes the answer with a challenge frame. The
        proof-of-work variants (``hashcash``, ``copilot``) are computed in-process
        (:mod:`copilot.challenges`). A ``None`` return means the challenge needs a
        browser-solved token and the caller must surface that.

        An *empty* challenge (``method``/``parameter`` both null) is NOT a no-op:
        capturing the real web client showed it answers ``{method:null}`` with a
        ``method:"cloudflare"`` Turnstile token. It only appears when
        ``cf_clearance`` is stale, and curl_cffi can't mint a Turnstile token — so
        we return ``None`` (the caller raises :class:`ClearanceRequired`). The old
        "ack an empty challenge with an empty token" behaviour was wrong: it made
        the socket wait for a token that never came and silently time out.
        """
        method = msg.get("method")
        parameter = msg.get("parameter")
        if method == "hashcash" and parameter:
            return solve_hashcash(parameter)
        if method == "copilot" and parameter:
            return solve_copilot_challenge(parameter)
        # method:null / 'cloudflare' (Turnstile) / unknown PoW: browser-only token.
        return None
