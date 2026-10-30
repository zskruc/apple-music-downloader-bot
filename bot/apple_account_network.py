"""Bounded account initialization recovery for the reviewed GAMDL release.

Install only inside the one-job GAMDL subprocess.  A successful fallback becomes
that subprocess's route for the API clients GAMDL constructs afterwards.
"""
from __future__ import annotations

import asyncio
import ast
import contextvars
import functools
import importlib.metadata
import inspect
import json
import os
import re
import textwrap
import urllib.parse
import urllib.request
from pathlib import Path


REVIEWED_GAMDL_VERSION = "3.9.1"
FALLBACK_PROXIES_ENV = "GAMDL_ACCOUNT_FALLBACK_PROXY_URLS"
ATTEMPTS = 3
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_PATCH_MARKER = "_apple_account_network_patch_version"
_LOG_INSPECTION_BYTES = 128 * 1024
_ACCOUNT_EVENT_RE = re.compile(
    r"\[apple-music-ingest\] account request (failed|succeeded):([^\r\n]*)"
)
_RUN_START_RE = re.compile(r"(?m)^(?:\$ |[^\r\n]*\bStarting Gamdl\b)")


def is_terminal_account_auth_failure(log_file: Path, tail: list[str]) -> bool:
    """Recognize the current request's explicit auth rejection, never old text.

    GAMDL appends retries to one file.  Inspect only the bounded end plus the
    current subprocess tail, discard earlier invocation sections, and let a
    later successful account request supersede an earlier rejection.
    """
    try:
        with log_file.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - _LOG_INSPECTION_BYTES))
            text = handle.read(_LOG_INSPECTION_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        text = ""
    text += "\n" + "\n".join(tail)[-_LOG_INSPECTION_BYTES:]
    starts = list(_RUN_START_RE.finditer(text))
    if starts:
        text = text[starts[-1].start():]
    events = list(_ACCOUNT_EVENT_RE.finditer(text))
    if not events or events[-1].group(1) != "failed":
        return False
    return re.search(r"(?:^|\s)status=(?:401|403)(?:\s|$)", events[-1].group(2)) is not None


def _fallback_proxies() -> list[str]:
    """Validate configuration without exposing proxy URLs in error messages."""
    try:
        values = json.loads(os.environ.get(FALLBACK_PROXIES_ENV, "[]"))
        if not isinstance(values, list) or len(values) > 8:
            raise ValueError
        proxies: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError
            parsed = urllib.parse.urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or any(character.isspace() for character in value)
            ):
                raise ValueError
            # Accessing port also validates malformed and out-of-range values.
            _ = parsed.port
            if value not in proxies:
                proxies.append(value)
        return proxies
    except (TypeError, ValueError):
        raise RuntimeError(
            f"{FALLBACK_PROXIES_ENV} must be a JSON array of valid HTTP proxy URLs"
        ) from None


def _check_supported_method(api_class: type) -> bool:
    """Return False for our installed patch; reject unreviewed upstream code."""
    try:
        installed_version = importlib.metadata.version("gamdl")
    except importlib.metadata.PackageNotFoundError:
        raise RuntimeError("Apple account network patch cannot identify GAMDL") from None
    if installed_version != REVIEWED_GAMDL_VERSION:
        raise RuntimeError(
            "Apple account network patch requires reviewed GAMDL 3.9.1; "
            "review the upstream account implementation before upgrading"
        )
    descriptor = api_class.__dict__.get("get_account_info")
    if not isinstance(descriptor, staticmethod):
        raise RuntimeError("Unsupported GAMDL get_account_info descriptor")
    original = descriptor.__func__
    if getattr(original, _PATCH_MARKER, None) == REVIEWED_GAMDL_VERSION:
        return False
    parameters = list(inspect.signature(original).parameters.values())
    if (
        not inspect.iscoroutinefunction(original)
        or [parameter.name for parameter in parameters]
        != ["token", "media_user_token", "meta"]
        or any(
            parameter.kind != inspect.Parameter.POSITIONAL_OR_KEYWORD
            for parameter in parameters
        )
        or parameters[0].default is not inspect.Parameter.empty
        or parameters[1].default is not inspect.Parameter.empty
        or parameters[2].default != "subscription"
    ):
        raise RuntimeError("Unsupported GAMDL get_account_info signature")
    try:
        source = inspect.getsource(original)
    except (OSError, TypeError):
        raise RuntimeError("Cannot verify GAMDL get_account_info source") from None
    required_fragments = (
        "async with httpx.AsyncClient() as client:",
        "response = await client.get(",
        "APPLE_MUSIC_AMP_API_URL + APPLE_MUSIC_ACCOUNT_INFO_API_URI",
        '"authorization": f"Bearer {token}"',
        '"cookie": f"media-user-token={media_user_token}"',
        "response.raise_for_status()",
        "account_info = response.json()",
        "except httpx.HTTPError:",
        "raise GamdlApiResponseError(",
        '"Error fetching account info"',
        "return account_info",
    )
    if not all(fragment in source for fragment in required_fragments):
        raise RuntimeError(
            "Unsupported GAMDL get_account_info implementation; "
            "review the upstream source before installing this patch"
        )
    return True


def _check_supported_create(upstream):
    """Verify the API factory whose custom transport ignores proxy variables."""
    descriptor = upstream.AppleMusicApi.__dict__.get("create")
    if not isinstance(descriptor, classmethod):
        raise RuntimeError("Unsupported GAMDL AppleMusicApi.create descriptor")
    original = descriptor.__func__
    if getattr(original, _PATCH_MARKER, None) == REVIEWED_GAMDL_VERSION:
        return None
    parameters = list(inspect.signature(original).parameters.values())
    expected_defaults = [inspect.Parameter.empty, "us", "en-US", None, None]
    if (
        not inspect.iscoroutinefunction(original)
        or [parameter.name for parameter in parameters]
        != ["cls", "storefront", "language", "token", "media_user_token"]
        or any(parameter.kind != inspect.Parameter.POSITIONAL_OR_KEYWORD for parameter in parameters)
        or [parameter.default for parameter in parameters] != expected_defaults
    ):
        raise RuntimeError("Unsupported GAMDL AppleMusicApi.create signature")
    try:
        source = textwrap.dedent(inspect.getsource(original))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        raise RuntimeError("Cannot verify GAMDL AppleMusicApi.create source") from None
    retry_clients = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "httpx"
            and node.func.attr == "AsyncClient"
        ):
            continue
        options = {keyword.arg: keyword.value for keyword in node.keywords}
        transport = options.get("transport")
        if not (
            isinstance(transport, ast.Call)
            and isinstance(transport.func, ast.Name)
            and transport.func.id == "RetryTransport"
        ):
            continue
        retry = next((keyword.value for keyword in transport.keywords if keyword.arg == "retry"), None)
        if not (isinstance(retry, ast.Call) and isinstance(retry.func, ast.Name) and retry.func.id == "Retry"):
            continue
        try:
            retry_options = {keyword.arg: ast.literal_eval(keyword.value) for keyword in retry.keywords}
        except (ValueError, TypeError):
            continue
        if retry_options == {"total": 6, "backoff_factor": 1, "status_forcelist": [429, 500, 502, 503, 504]}:
            retry_clients.append(node)
    if (
        len(retry_clients) != 1
        or "await cls.get_account_info(token, media_user_token)" not in source
        or "client=client" not in source.replace(" ", "").replace("\n", "")
    ):
        raise RuntimeError("Unsupported GAMDL AppleMusicApi.create implementation")
    transport_parameters = list(inspect.signature(upstream.RetryTransport.__init__).parameters.values())
    if (
        [parameter.name for parameter in transport_parameters] != ["self", "transport", "retry"]
        or [parameter.default for parameter in transport_parameters]
        != [inspect.Parameter.empty, None, None]
    ):
        raise RuntimeError("Unsupported GAMDL RetryTransport constructor")
    return original


def _environment_proxy(api_url: str) -> str | None:
    proxies = urllib.request.getproxies_environment()
    host = urllib.parse.urlsplit(api_url).hostname
    if host and urllib.request.proxy_bypass_environment(host, proxies):
        return None
    return proxies.get("https") or proxies.get("all") or None


async def _bind_api_transport(api, proxy: str, upstream) -> None:
    """Keep the existing client configuration and Retry while pinning its route."""
    client = api.client
    previous = getattr(client, "_transport", None)
    mounts = getattr(client, "_mounts", None)
    if (
        not isinstance(previous, upstream.RetryTransport)
        or not hasattr(previous, "retry")
        or not isinstance(mounts, dict)
        or mounts
    ):
        await client.aclose()
        raise RuntimeError("Unsupported GAMDL API client transport layout")
    replacement = None
    try:
        replacement = upstream.RetryTransport(
            transport=upstream.httpx.AsyncHTTPTransport(proxy=proxy),
            retry=previous.retry,
        )
        await previous.aclose()
    except BaseException:
        if replacement is not None:
            await replacement.aclose()
        await client.aclose()
        raise
    client._transport = replacement
    print("[apple-music-ingest] API client pinned to successful account proxy; retry policy retained", flush=True)


def install_account_network_patch() -> None:
    """Install the release-guarded patch, preserving GAMDL's error contract."""
    from gamdl.api import apple_music as upstream

    proxies = _fallback_proxies()
    install_account = _check_supported_method(upstream.AppleMusicApi)
    original_create = _check_supported_create(upstream)
    if not install_account and original_create is None:
        return
    if not install_account or original_create is None:
        raise RuntimeError("Apple account network patch is only partially installed")
    httpx = upstream.httpx
    timeout = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=15.0)
    successful_proxy = contextvars.ContextVar("gamdl_successful_account_proxy", default=None)

    async def get_account_info(
        token: str,
        media_user_token: str,
        meta: str = "subscription",
    ) -> dict:
        for attempt in range(ATTEMPTS):
            proxy = proxies[(attempt - 1) % len(proxies)] if attempt and proxies else None
            route = f"fallback-{((attempt - 1) % len(proxies)) + 1}" if proxy else "environment"
            response = None
            options = {"timeout": timeout, "trust_env": proxy is None}
            if proxy is not None:
                options["proxy"] = proxy
            try:
                async with httpx.AsyncClient(**options) as client:
                    response = await client.get(
                        upstream.APPLE_MUSIC_AMP_API_URL
                        + upstream.APPLE_MUSIC_ACCOUNT_INFO_API_URI,
                        params={"meta": meta},
                        headers={
                            "authorization": f"Bearer {token}",
                            "origin": upstream.APPLE_MUSIC_HOMEPAGE_URL,
                            "cookie": f"media-user-token={media_user_token}",
                        },
                    )
                    response.raise_for_status()
                    account_info = response.json()
            except httpx.HTTPError as exc:
                status = response.status_code if response is not None else None
                transient = isinstance(
                    exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError)
                ) or status in RETRYABLE_STATUSES
                print(
                    "[apple-music-ingest] account request failed: "
                    f"attempt={attempt + 1}/{ATTEMPTS} route={route} "
                    f"error={type(exc).__name__} status={status}",
                    flush=True,
                )
                if not transient or attempt + 1 >= ATTEMPTS:
                    # The exception's public fields match upstream.  Suppress
                    # arbitrary transport error text, which can contain proxy
                    # credentials, while retaining safe diagnostics above.
                    raise upstream.GamdlApiResponseError(
                        "Error fetching account info", status_code=status
                    ) from None
                await asyncio.sleep(float(2 ** (attempt + 1)))
                continue
            if proxy is not None:
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                    os.environ[name] = proxy
            successful_proxy.set(proxy or _environment_proxy(upstream.APPLE_MUSIC_AMP_API_URL))
            print(
                "[apple-music-ingest] account request succeeded: "
                f"attempt={attempt + 1}/{ATTEMPTS} route={route}",
                flush=True,
            )
            return account_info
        raise AssertionError("Account request attempts exhausted without a result")

    @functools.wraps(original_create)
    async def create(cls, *args, **kwargs):
        context_token = successful_proxy.set(None)
        try:
            api = await original_create(cls, *args, **kwargs)
            selected_proxy = successful_proxy.get()
            if selected_proxy is not None:
                await _bind_api_transport(api, selected_proxy, upstream)
            return api
        finally:
            successful_proxy.reset(context_token)

    setattr(get_account_info, _PATCH_MARKER, REVIEWED_GAMDL_VERSION)
    setattr(create, _PATCH_MARKER, REVIEWED_GAMDL_VERSION)
    upstream.AppleMusicApi.get_account_info = staticmethod(get_account_info)
    upstream.AppleMusicApi.create = classmethod(create)
