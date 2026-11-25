"""Run MCP client sessions in the background so synchronous Flask code can use them.

The MCP Python SDK is async-only, while Flask handles each request on a normal
thread. This module keeps one asyncio event loop on a daemon thread and, for each
data provider, one long-lived MCP session on that loop:

* ``McpConnection`` owns the session. A single task enters the SDK's context
  managers and serves tool calls from a queue, because anyio cancel scopes must be
  entered and exited by the same task. Keeping the session open also matters for
  Kite, whose login is tied to the MCP session id.
* ``OAuthHandshake`` connects the SDK's interactive OAuth steps (open a URL, wait
  for the redirect back) to the web app's ``/oauth/<provider>/callback`` route.
* ``FileTokenStorage`` saves tokens so a restart does not require logging in again.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp import Client, MCPError
from mcp.client.auth import OAuthClientProvider, OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.types import CallToolResult, Implementation

try:  # Python 3.11+
    _BaseExceptionGroup = BaseExceptionGroup  # type: ignore[name-defined]
except NameError:  # pragma: no cover - Python 3.10 (anyio depends on the backport)
    from exceptiongroup import BaseExceptionGroup as _BaseExceptionGroup

log = logging.getLogger(__name__)

CLIENT_INFO = Implementation(name="stock-data-python", version="1.0.0")
CONNECT_TIMEOUT = 30.0  # seconds to wait for a session to be ready
CALL_TIMEOUT = 60.0  # seconds for one tool call
AUTH_TIMEOUT = 600.0  # seconds the user has to finish logging in

# Connection states, as reported to the web UI.
IDLE = "idle"
CONNECTING = "connecting"
AUTHORIZING = "authorizing"  # waiting for the user to log in at auth_url
CONNECTED = "connected"
FAILED = "failed"

_STOP = object()  # queue sentinel: close the session
_LOST = object()  # queue sentinel: the server dropped the session; reconnect


class BridgeError(Exception):
    """Base class for errors raised by this module."""


class AuthorizationRequired(BridgeError):
    """The provider needs the user to log in before it can be used."""

    def __init__(self, auth_url: str | None):
        super().__init__("Login required")
        self.auth_url = auth_url


class ConnectionFailed(BridgeError):
    """The MCP session could not be established or was lost."""


class CallTimeout(BridgeError):
    """A tool call did not finish in time."""


class LoginAbandoned(OAuthFlowError):
    """The user did not finish logging in (timed out or cancelled); not shown as an error."""


class EventLoopThread:
    """A daemon thread running an asyncio event loop, started on first use."""

    def __init__(self, name: str = "mcp-bridge"):
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name=self._name, daemon=True).start()
                self._loop = loop
            return self._loop

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call_soon(self, fn: Callable[..., Any], *args: Any) -> None:
        self.loop.call_soon_threadsafe(fn, *args)


class FileTokenStorage:
    """Stores one provider's OAuth tokens and client registration in a JSON file.

    Implements the SDK's ``TokenStorage`` protocol. The file holds credentials,
    so it is created readable by the owner only.
    """

    def __init__(self, path: Path, redirect_uri: str):
        self.path = Path(path)
        self.redirect_uri = redirect_uri
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("Ignoring unreadable token file %s", self.path)
            return {}

    def _update(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            data[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, self.path)

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._update("tokens", tokens.model_dump(mode="json", exclude_none=True))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client_info")
        if not raw:
            return None
        info = OAuthClientInformationFull.model_validate(raw)
        registered = [str(uri) for uri in (info.redirect_uris or [])]
        if registered and self.redirect_uri not in registered:
            # The app now runs on a different address: register a new client.
            return None
        return info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._update("client_info", client_info.model_dump(mode="json", exclude_none=True))

    def has_tokens(self) -> bool:
        return bool(self._read().get("tokens"))

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)


class MemoryTokenStorage(FileTokenStorage):
    """Like ``FileTokenStorage`` but keeps tokens in memory only (nothing on disk).

    Used by the hosted Streamlit app, where each visitor's logins must stay in
    their own session and vanish when it ends.
    """

    def __init__(self, redirect_uri: str):
        super().__init__(Path(os.devnull), redirect_uri)
        self._data: dict[str, Any] = {}

    def _read(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def _update(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class OAuthHandshake:
    """Hands the SDK's authorization URL to the web UI and waits for the redirect back."""

    def __init__(self, loop_thread: EventLoopThread, on_pending: Callable[[str | None], None]):
        self._loop_thread = loop_thread
        self._on_pending = on_pending
        self._future: asyncio.Future[AuthorizationCodeResult] | None = None
        self.pending_url: str | None = None

    async def redirect_handler(self, authorization_url: str) -> None:
        self._future = asyncio.get_running_loop().create_future()
        self.pending_url = authorization_url
        self._on_pending(authorization_url)

    async def callback_handler(self) -> AuthorizationCodeResult:
        future = self._future
        if future is None:
            raise OAuthFlowError("Authorization was not started")
        try:
            with anyio.fail_after(AUTH_TIMEOUT):
                return await future
        except TimeoutError:
            raise LoginAbandoned("Login was not completed in time.") from None
        finally:
            self._future = None
            self.pending_url = None
            self._on_pending(None)

    @property
    def waiting(self) -> bool:
        future = self._future
        return future is not None and not future.done()

    def complete(
        self,
        *,
        code: str | None,
        state: str | None,
        iss: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> bool:
        """Deliver the provider's redirect (called from a Flask thread).

        Returns False when no login is in progress. The SDK checks ``state``.
        """
        future = self._future
        if future is None or future.done():
            return False

        def resolve() -> None:
            if future.done():
                return
            if error or not code:
                future.set_exception(OAuthFlowError(error_description or error or "No authorization code received"))
            else:
                future.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))

        self._loop_thread.call_soon(resolve)
        return True

    def cancel(self) -> None:
        """Abandon a login in progress (called from a Flask thread)."""
        future = self._future
        if future is not None and not future.done():
            self._loop_thread.call_soon(lambda: future.done() or future.set_exception(LoginAbandoned("Login cancelled.")))


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, _BaseExceptionGroup):
        leaves: list[BaseException] = []
        for inner in exc.exceptions:
            leaves.extend(_leaf_exceptions(inner))
        return leaves
    return [exc]


def describe_error(exc: BaseException, url: str) -> str:
    """Turn a connection error (possibly wrapped in exception groups) into a short message."""
    host = urlsplit(url).hostname or url
    leaves = _leaf_exceptions(exc)
    # Prefer the most informative error when tasks failed together.
    for kind in (OAuthFlowError, OAuthRegistrationError, OAuthTokenError, httpx2.HTTPStatusError, MCPError):
        for leaf in leaves:
            if isinstance(leaf, kind):
                exc = leaf
                break
        else:
            continue
        break
    else:
        exc = leaves[0]

    if isinstance(exc, (OAuthFlowError, OAuthRegistrationError, OAuthTokenError)):
        return f"Login with {host} failed: {exc}"
    if isinstance(exc, httpx2.HTTPStatusError):
        return f"{host} answered HTTP {exc.response.status_code}."
    if isinstance(exc, (httpx2.TimeoutException, TimeoutError)):
        return f"Timed out talking to {host}."
    if isinstance(exc, httpx2.TransportError):
        return f"Could not reach {host}. Check your internet connection and the server URL."
    if isinstance(exc, MCPError):
        return f"{host}: {exc}"
    return f"{host}: {type(exc).__name__}: {exc}"


_SESSION_LOST_RE = re.compile(
    r"session (?:not found|terminated|expired|is invalid)|(?:invalid|unknown|expired) session|connection closed",
    re.IGNORECASE,
)


def _session_lost(exc: MCPError) -> bool:
    """True for errors meaning the session is gone (not ordinary server errors).

    Servers word this differently ("Session not found" after a restart, the SDK's
    own "Session terminated" for HTTP 404), so match the common phrasings.
    """
    return bool(_SESSION_LOST_RE.search(str(exc)))


def _settle(future: concurrent.futures.Future, *, result: Any = None, error: BaseException | None = None) -> None:
    if future.done():  # the caller gave up waiting
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)


class McpConnection:
    """One provider's MCP session, opened on demand and kept open between calls."""

    def __init__(
        self,
        name: str,
        url: str,
        loop_thread: EventLoopThread,
        *,
        storage: FileTokenStorage | None = None,
        client_name: str = "Stock Data Python",
    ):
        self.name = name
        self.url = url
        self.storage = storage
        self._loop_thread = loop_thread
        self._cond = threading.Condition()
        self._state = IDLE
        self._error: str | None = None
        self._queue: asyncio.Queue | None = None
        self._runner: concurrent.futures.Future | None = None
        self._client_name = client_name
        self.oauth: OAuthHandshake | None = None
        self._auth: OAuthClientProvider | None = None
        if storage is not None:
            self.oauth = OAuthHandshake(loop_thread, self._on_auth_pending)
            self._auth = self._make_auth()

    def _make_auth(self) -> OAuthClientProvider:
        """OAuth for servers that ask for it (HTTP 401); unused by servers that don't."""
        assert self.storage is not None and self.oauth is not None
        return OAuthClientProvider(
            server_url=self.url,
            client_metadata=OAuthClientMetadata(
                client_name=self._client_name,
                redirect_uris=[self.storage.redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            ),
            storage=self.storage,
            redirect_handler=self.oauth.redirect_handler,
            callback_handler=self.oauth.callback_handler,
        )

    # -- state -------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._cond:
            self._state = state
            self._error = error
            self._cond.notify_all()

    def _on_auth_pending(self, url: str | None) -> None:
        if url:
            self._set_state(AUTHORIZING)
        elif self._state == AUTHORIZING:
            self._set_state(CONNECTING)

    def status(self) -> dict[str, Any]:
        with self._cond:
            info: dict[str, Any] = {"state": self._state, "error": self._error}
        if info["state"] == AUTHORIZING and self.oauth is not None:
            info["auth_url"] = self.oauth.pending_url
        info["saved_login"] = bool(self.storage and self.storage.has_tokens())
        return info

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Open the session in the background if it is not already open or opening."""
        with self._cond:
            if self._runner is not None and not self._runner.done():
                return
            self._state = CONNECTING
            self._error = None
            self._cond.notify_all()
            self._runner = self._loop_thread.submit(self._run())

    def wait(self, timeout: float = CONNECT_TIMEOUT, *, through_login: bool = False) -> str:
        """Block while the session is connecting; return the state reached.

        By default this returns as soon as a login is needed. With
        ``through_login`` it also waits while a login is being completed (used
        by the OAuth callback, which runs before the SDK exchanges the code).
        """
        pending = (CONNECTING, AUTHORIZING) if through_login else (CONNECTING,)
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._state in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return self._state

    def connect(self, timeout: float = CONNECT_TIMEOUT) -> dict[str, Any]:
        self.start()
        self.wait(timeout)
        return self.status()

    def stop(self, forget_login: bool = False) -> None:
        """Close the session; with ``forget_login`` also delete saved tokens."""
        if self.oauth is not None:
            self.oauth.cancel()  # abandon a login in progress so the session task can finish
        self._loop_thread.call_soon(self._enqueue_stop)
        runner = self._runner
        if runner is not None:
            try:
                runner.result(timeout=10)
            except Exception:  # noqa: BLE001 - shutting down regardless
                pass
        if forget_login and self.storage is not None:
            self.storage.clear()
            self._auth = self._make_auth()  # drop tokens the SDK holds in memory
        self._set_state(IDLE)

    def _enqueue_stop(self) -> None:
        if self._queue is not None:
            self._queue.put_nowait(_STOP)

    # -- tool calls --------------------------------------------------------

    def call_tool(self, tool: str, arguments: dict[str, Any] | None = None, timeout: float = CALL_TIMEOUT) -> CallToolResult:
        """Call one tool and return the SDK's ``CallToolResult`` (thread-safe, blocking)."""
        for attempt in range(2):
            self._ensure_connected()
            future: concurrent.futures.Future = concurrent.futures.Future()
            self._loop_thread.call_soon(self._enqueue, _Call(tool, arguments or {}, future, timeout))
            try:
                return future.result(2 * timeout + CONNECT_TIMEOUT)  # allows for one reconnect
            except concurrent.futures.TimeoutError:
                future.cancel()
                raise CallTimeout(f"{self.name}: '{tool}' took longer than {timeout:.0f}s") from None
            except _SessionClosed:
                if attempt:
                    raise ConnectionFailed(f"Lost the connection to {self.name}") from None
                # The session task ended just before this call was queued: open a new one.
        raise AssertionError("unreachable")

    def _ensure_connected(self) -> None:
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while True:
            if self._state == CONNECTED:
                return
            self.start()
            state = self.wait(max(0.0, deadline - time.monotonic()))
            if state == CONNECTED:
                return
            if state == AUTHORIZING:
                raise AuthorizationRequired(self.oauth.pending_url if self.oauth else None)
            if state == FAILED:
                raise ConnectionFailed(self._error or f"Could not connect to {self.name}")
            if time.monotonic() >= deadline:
                raise ConnectionFailed(f"Timed out connecting to {self.name}")
            time.sleep(0.05)  # IDLE: the previous session task is just finishing

    def _enqueue(self, call: _Call) -> None:
        # Runs on the event loop thread, like the session task, so it cannot race with shutdown.
        if self._queue is None:
            _settle(call.future, error=_SessionClosed())
        else:
            self._queue.put_nowait(call)

    # -- the session task --------------------------------------------------

    async def _run(self) -> None:
        """Own the session: open it, serve calls, and reopen it if the server drops it."""
        queue: asyncio.Queue = asyncio.Queue()
        self._queue = queue
        final_state, final_error = IDLE, None
        try:
            while await self._session(queue):
                log.info("Session with %s was lost; reconnecting", self.name)
                self._set_state(CONNECTING)
        except Exception as exc:  # noqa: BLE001 - reported to the UI
            if not any(isinstance(leaf, LoginAbandoned) for leaf in _leaf_exceptions(exc)):
                final_state = FAILED
                final_error = describe_error(exc, self.url)  # names the host only: URLs may embed keys
                log.warning("MCP session for %s ended: %s", self.name, final_error)
        finally:
            if self._queue is queue:
                self._queue = None
            reason = ConnectionFailed(final_error or f"{self.name} was disconnected")
            while not queue.empty():
                item = queue.get_nowait()
                if isinstance(item, _Call):
                    _settle(item.future, error=reason)
            self._set_state(final_state, final_error)

    async def _session(self, queue: asyncio.Queue) -> bool:
        """Serve calls on one MCP session. Returns True if the session was lost."""
        lost = _Flag()
        timeout = httpx2.Timeout(30.0, read=300.0)
        async with httpx2.AsyncClient(auth=self._auth, timeout=timeout) as http:
            transport = streamable_http_client(self.url, http_client=http)
            async with Client(transport, mode="legacy", client_info=CLIENT_INFO) as client:
                self._set_state(CONNECTED)
                async with anyio.create_task_group() as tasks:
                    while True:
                        item = await queue.get()
                        if item is _STOP:
                            tasks.cancel_scope.cancel()
                            return False
                        if item is _LOST:
                            break  # in-flight calls finish or re-queue themselves first
                        tasks.start_soon(self._serve, client, queue, item, lost)
        return True

    async def _serve(self, client: Client, queue: asyncio.Queue, call: _Call, lost: _Flag) -> None:
        if call.future.done():  # the caller stopped waiting
            return
        try:
            result = await client.call_tool(call.tool, call.arguments, read_timeout_seconds=call.timeout)
        except MCPError as exc:
            if _session_lost(exc):
                self._retry_later(queue, call, lost, ConnectionFailed(f"Lost the session with {self.name}"))
            else:
                _settle(call.future, error=exc)
        except Exception as exc:  # noqa: BLE001 - transport failure: reconnect and retry once
            self._retry_later(queue, call, lost, ConnectionFailed(describe_error(exc, self.url)))
        except BaseException:  # cancelled: the session is being closed
            _settle(call.future, error=ConnectionFailed(f"{self.name} was disconnected"))
            raise
        else:
            _settle(call.future, result=result)

    @staticmethod
    def _retry_later(queue: asyncio.Queue, call: _Call, lost: _Flag, error: BaseException) -> None:
        if call.attempts:
            _settle(call.future, error=error)
            return
        call.attempts += 1
        if not lost.set:
            lost.set = True
            queue.put_nowait(_LOST)  # once per session, however many calls failed
        queue.put_nowait(call)  # served again by the next session


class _Call:
    """One queued tool call."""

    __slots__ = ("tool", "arguments", "future", "timeout", "attempts")

    def __init__(self, tool: str, arguments: dict[str, Any], future: concurrent.futures.Future, timeout: float):
        self.tool = tool
        self.arguments = arguments
        self.future = future
        self.timeout = timeout
        self.attempts = 0


class _Flag:
    __slots__ = ("set",)

    def __init__(self) -> None:
        self.set = False


class _SessionClosed(BridgeError):
    """Internal: the session closed before the call ran; safe to retry."""
