"""Deterministic in-process fakes for the aiohttp surface ``HTTPEngine`` touches.

``HTTPEngine`` was previously exercised only through its pure helpers, so ~540 of its
819 statements - the entire download pipeline - were untested. Reaching them offline
needs a stand-in for exactly four aiohttp behaviours:

* ``session.head(url, allow_redirects=True, **kwargs)`` used as an async context manager
  yielding a response with ``status`` / ``headers`` / ``url``;
* ``session.get(url, **kwargs)`` used the same way, plus
  ``resp.content.iter_chunked(size)`` as an async generator;
* raising a real ``aiohttp.ClientError`` / ``OSError`` / ``asyncio.TimeoutError`` so the
  engine's own retry ladder is the thing under test;
* ``await session.close()`` so ``stop()`` and ``_recreate_session()`` can run.

``FakeSession`` therefore serves a scripted list of responses per verb and records every
call, so a test can assert the *exact* request the engine built (headers, Range, proxy)
rather than only that the download finished.

The fakes are intentionally strict: a scripted response is consumed at most once and an
unscripted request fails loudly. A test that silently reuses a response would otherwise
pass for the wrong reason, which is exactly the class of bug these tests exist to catch.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Iterable, Optional


class FakeStream:
    """Minimal stand-in for ``resp.content`` with ``iter_chunked``."""

    def __init__(
        self,
        chunks: Iterable[bytes],
        on_chunk: Optional[Callable] = None,
        raise_after_chunks: Optional[BaseException] = None,
    ):
        self._chunks = list(chunks)
        self._on_chunk = on_chunk
        self._raise_after_chunks = raise_after_chunks

    def iter_chunked(self, size: int = 0):
        async def _gen():
            for chunk in self._chunks:
                if self._on_chunk is not None:
                    # Lets a test flip a cancel event exactly between chunks, which is the
                    # only way to reach the mid-transfer `cancel_evt.is_set()` branches.
                    self._on_chunk(chunk)
                yield chunk
            if self._raise_after_chunks is not None:
                # The whole promised body was delivered and then the connection died -
                # the case the engine's resume-on-retry logic has to survive.
                raise self._raise_after_chunks

        return _gen()

    def iter_any(self):
        return self.iter_chunked(0)


class FakeResponse:
    """A scripted HTTP response.

    ``chunks`` are yielded by ``content.iter_chunked``; ``on_chunk`` runs before each one
    is yielded, so a test can flip a cancel event exactly between chunks.
    ``raise_on_enter`` simulates a transport failure *before* any body is read (a refused
    connection); ``raise_after_chunks`` simulates a body that dies mid-transfer, which is
    what the engine's resume-on-retry logic has to cope with.
    """

    def __init__(
        self,
        status: int = 200,
        headers: Optional[dict] = None,
        chunks: Iterable[bytes] = (),
        url: str = "https://example.com/a.zip",
        on_chunk: Optional[Callable] = None,
        raise_on_enter: Optional[BaseException] = None,
        raise_after_chunks: Optional[BaseException] = None,
    ):
        self.status = status
        self.headers = dict(headers or {})
        self.url = url
        self.content = FakeStream(
            chunks, on_chunk=on_chunk, raise_after_chunks=raise_after_chunks
        )
        self._raise_on_enter = raise_on_enter


class _ResponseContext:
    """Async context manager returned by ``FakeSession.head`` / ``.get``."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        if self._response._raise_on_enter is not None:
            raise self._response._raise_on_enter
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ScriptExhausted(AssertionError):
    pass


class FakeSession:
    """Serves scripted responses and records the requests the engine issued."""

    def __init__(
        self,
        heads: Optional[list] = None,
        gets: Optional[list] = None,
        closed: bool = False,
    ):
        # A callable entry is invoked to build the response, so a test can vary the
        # script per call (used for the "first attempt fails, second succeeds" retries).
        self._heads = list(heads or [])
        self._gets = list(gets or [])
        self.closed = closed
        self.head_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.close_calls = 0

    # -- scripting ------------------------------------------------------------

    def _next(self, script, calls, verb):
        if not script:
            raise _ScriptExhausted(
                f"FakeSession received an unscripted {verb}() call; "
                f"previous calls: {calls!r}"
            )
        item = script.pop(0)
        return item() if callable(item) else item

    def queue_head(self, response):
        self._heads.append(response)
        return self

    def queue_get(self, response):
        self._gets.append(response)
        return self

    # -- aiohttp surface ------------------------------------------------------

    def head(self, url, **kwargs):
        self.head_calls.append({"url": url, **kwargs})
        return _ResponseContext(self._next(self._heads, self.head_calls, "head"))

    def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        return _ResponseContext(self._next(self._gets, self.get_calls, "get"))

    async def close(self):
        self.close_calls += 1
        self.closed = True

    # -- assertions helpers ---------------------------------------------------

    @property
    def pending(self) -> int:
        return len(self._heads) + len(self._gets)

    def ranges(self) -> list[Optional[str]]:
        return [call["headers"].get("Range") for call in self.get_calls]


class FakeCurlResponse:
    """Scripted ``curl_cffi`` response (only ``aiter_content`` is used by the engine)."""

    def __init__(
        self,
        status_code: int = 200,
        headers: Optional[dict] = None,
        chunks: Iterable[bytes] = (),
        on_chunk: Optional[Callable] = None,
    ):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self._on_chunk = on_chunk

    async def aiter_content(self, size: int = 0):
        for chunk in self._chunks:
            if self._on_chunk is not None:
                self._on_chunk(chunk)
            yield chunk

class FakeCurlSession:
    """Async context manager standing in for ``curl_cffi.requests.AsyncSession``."""

    #: Every constructed instance, so a test can assert how many probe/download
    #: attempts actually went through the impersonation path.
    instances: list["FakeCurlSession"] = []

    def __init__(self, responses: Optional[list] = None, raise_on_get: Optional[BaseException] = None):
        self._responses = list(responses or [])
        self._raise_on_get = raise_on_get
        self.get_calls: list[dict] = []
        self.kwargs: dict[str, Any] = {}
        FakeCurlSession.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        self.get_calls.append({"url": url, **kwargs})
        if self._raise_on_get is not None:
            raise self._raise_on_get
        if not self._responses:
            raise _ScriptExhausted("FakeCurlSession received an unscripted get()")
        item = self._responses.pop(0)
        return item() if callable(item) else item


def curl_session_factory(responses: Optional[list] = None, raise_on_get: Optional[BaseException] = None):
    """Build a drop-in replacement for ``my_idm.http_engine.CurlAsyncSession``."""
    created: list[FakeCurlSession] = []

    def factory(*args, **kwargs):
        session = FakeCurlSession(responses, raise_on_get=raise_on_get)
        session.kwargs = dict(kwargs)
        created.append(session)
        return session

    factory.created = created  # type: ignore[attr-defined]
    return factory


def run_async(coro):
    """Run *coro* on a private event loop.

    ``asyncio.run`` is used deliberately: it creates and closes a fresh loop every call, so
    no ``asyncio.Event`` / lock / task created by a previous test can leak in and make the
    suite order-dependent.
    """
    return asyncio.run(coro)
