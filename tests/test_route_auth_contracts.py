"""Default-deny contract: every ``/api/v1/**`` route must gate itself somehow.

Walks the *live* FastAPI dependency graph (``route.dependant``, recursively)
rather than grepping source for ``require_auth`` by name -- a route whose
gate got silently dropped in a refactor (or added new without one) fails
this test even if some other file still mentions the dependency's name in a
comment or docstring. Everything not on the explicit allowlist below, with a
reason, must carry :func:`~palmimo_portal.api.deps.require_auth` somewhere
in its dependency tree; the Wi-Fi router's routes gate themselves with
:func:`~palmimo_portal.api.deps.require_wifi_access` AND
:func:`~palmimo_portal.api.deps.require_full_session` instead (see those
functions' docstrings for why), so those are checked for both dependencies
in particular rather than being an unconditional allowlist entry.

Dependencies are matched by identity (``sub.call is require_auth``), not by
``__name__`` string comparison -- a name collision with some unrelated
function would otherwise be able to satisfy this check without actually
being the real gate.

Runs against fake adapters (the ``app``/``settings`` fixtures from
``conftest.py``) -- this test never sends a request, only inspects the
route table FastAPI/Starlette build at ``create_app()`` time, so which
adapters are wired in makes no difference to what it checks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from palmimo_portal.api import wifi as wifi_api
from palmimo_portal.api.app import create_app
from palmimo_portal.api.deps import require_auth, require_full_session, require_wifi_access
from palmimo_portal.settings import Settings


#: (method, path) -> reason. Every /api/v1/** route not listed here must
#: carry require_auth somewhere in its dependency tree.
ALLOWLISTED_UNAUTHENTICATED_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/system/status"): (
        "Always public -- the frontend needs it before a session exists, to "
        "decide which screen (setup/login/app) to show at all."
    ),
    ("POST", "/api/v1/auth/setup"): (
        "First-time password creation on a device with no password yet -- "
        "there is nothing to authenticate against before this succeeds."
    ),
    ("POST", "/api/v1/auth/login"): "The endpoint that produces a session in the first place.",
    ("POST", "/api/v1/auth/reset"): (
        "Deliberately unauthenticated on identity-carrying devices -- a "
        "forgotten owner-set password must not be able to permanently lock "
        "an owner out; see core.auth.decide_reset's and ResetRateLimiter's "
        "docstrings for the throttling that bounds the nuisance/DoS this "
        "opens up."
    ),
}

#: /api/v1/wifi/** routes gate themselves with require_wifi_access AND
#: require_full_session instead of require_auth (an identity-carrying
#: device is always session-gated there; a DIY device is deliberately
#: reachable anonymously while unprovisioned, since Wi-Fi setup has to be
#: possible before there is any session to present) -- see
#: require_wifi_access's docstring. Empty today, but kept as a named
#: allowlist (mirroring ALLOWLISTED_UNAUTHENTICATED_ROUTES above) so a
#: future wifi route that genuinely needs to skip one of the two gates has
#: a place to say so, with a reason, rather than silently falling through
#: this contract.
WIFI_ROUTES_EXEMPT_FROM_FULL_GATING: dict[tuple[str, str], str] = {}

#: /api/v1/wifi/** routes gate themselves with require_wifi_access instead
#: of require_auth (an identity-carrying device is always session-gated
#: there; a DIY device is deliberately reachable anonymously while
#: unprovisioned, since Wi-Fi setup has to be possible before there is any
#: session to present) -- see require_wifi_access's docstring.
WIFI_ACCESS_PREFIX = "/api/v1/wifi/"

API_PREFIX = "/api/"


def _iter_api_routes(app: FastAPI) -> Iterable[APIRoute]:
    """Yield every :class:`APIRoute` under :data:`API_PREFIX` FastAPI registered.

    Covers two shapes at once: FastAPI 0.141's ``app.routes`` holds one
    ``_IncludedRouter`` wrapper per ``include_router()`` call (its
    ``original_router.routes`` is the actual :class:`APIRoute` list, the
    same objects ``route.dependant`` lives on) -- but a route can also be
    registered directly on the app (``app.get(...)``/``app.post(...)``),
    which shows up as a bare :class:`APIRoute` straight in ``app.routes``
    with no wrapper at all. Missing that second shape would let a route
    added this way (bypassing every router's own dependencies entirely)
    slip past this contract silently. Filtered to :data:`API_PREFIX` so the
    SPA fallback routes (``/``, ``/{full_path:path}`` -- also bare
    :class:`APIRoute` objects, see :func:`~palmimo_portal.api.app._mount_frontend`)
    are never mistaken for an ungated API route.
    """
    for entry in app.routes:
        if isinstance(entry, APIRoute) and entry.path.startswith(API_PREFIX):
            yield entry
            continue
        router = getattr(entry, "original_router", None)
        if router is None:
            continue
        for route in router.routes:
            if isinstance(route, APIRoute) and route.path.startswith(API_PREFIX):
                yield route


def _has_dependency(dependant: Dependant, target: Callable[..., Any], *, _seen: set[int] | None = None) -> bool:
    """Report whether *target* (compared by identity) is reachable from *dependant*, recursively.

    ``_seen`` (by ``id()``) guards against revisiting the same
    sub-dependency object reached via two different paths -- harmless
    correctness-wise but avoids doing needless repeat work on a route with
    several dependencies that share a common sub-dependency (e.g.
    ``get_network_port``).
    """
    seen = _seen if _seen is not None else set()
    for sub in dependant.dependencies:
        if id(sub) in seen:
            continue
        seen.add(id(sub))
        if sub.call is target:
            return True
        if _has_dependency(sub, target, _seen=seen):
            return True
    return False


def _find_unexplained_routes(app: FastAPI) -> list[str]:
    """Return every ``/api/v1/**`` route that carries no auth gate and no allowlist entry.

    Pulled out as its own function (rather than living inline in the test
    body) so :func:`test_the_contract_function_itself_catches_an_ungated_route`
    below can drive it directly against a throwaway app -- a white-box check
    that this contract's own machinery actually fails closed, not just that
    today's real route table happens to pass it.
    """
    unexplained: list[str] = []
    for route in _iter_api_routes(app):
        for method in sorted(route.methods or ()):
            if method == "HEAD":
                continue  # FastAPI adds this automatically alongside GET; not a distinct route to gate.
            key = (method, route.path)
            if key in ALLOWLISTED_UNAUTHENTICATED_ROUTES:
                continue
            if route.path.startswith(WIFI_ACCESS_PREFIX):
                if key in WIFI_ROUTES_EXEMPT_FROM_FULL_GATING:
                    continue
                missing = [
                    dep.__name__
                    for dep in (require_wifi_access, require_full_session)
                    if not _has_dependency(route.dependant, dep)
                ]
                if missing:
                    unexplained.append(f"{method} {route.path} (wifi route missing {', '.join(missing)})")
                continue
            if not _has_dependency(route.dependant, require_auth):
                unexplained.append(f"{method} {route.path} (missing require_auth, and not allowlisted)")
    return unexplained


def test_every_api_route_is_allowlisted_or_carries_require_auth(app: FastAPI) -> None:
    routes = list(_iter_api_routes(app))
    assert routes, "sanity: the route table must not be empty"

    unexplained = _find_unexplained_routes(app)

    assert not unexplained, (
        "route(s) with no auth gate and no allowlist entry -- add require_auth, "
        f"or add a reasoned entry to ALLOWLISTED_UNAUTHENTICATED_ROUTES: {unexplained}"
    )


def test_the_contract_function_itself_catches_an_ungated_route() -> None:
    # A white-box self-test: build a throwaway app with a single, genuinely
    # ungated /api/v1/x route and confirm _find_unexplained_routes actually
    # flags it. Without this, a bug in the walk itself (e.g. the API_PREFIX
    # filter silently matching nothing) could make the real test above pass
    # vacuously -- the same failure mode the mount tests below had before
    # this rewrite.
    throwaway = FastAPI()

    @throwaway.get("/api/v1/x")
    def _ungated() -> dict[str, str]:  # pragma: no cover -- never actually called
        return {"status": "ok"}

    unexplained = _find_unexplained_routes(throwaway)

    assert unexplained == ["GET /api/v1/x (missing require_auth, and not allowlisted)"]


def test_the_allowlist_itself_only_names_routes_that_actually_exist(app: FastAPI) -> None:
    # Catches a stale allowlist entry (the route was renamed/removed) --
    # without this, a typo'd or dead entry would silently widen the
    # allowlist rather than erroring, defeating the point of the contract.
    live_keys = {
        (method, route.path) for route in _iter_api_routes(app) for method in (route.methods or ()) if method != "HEAD"
    }
    for key in ALLOWLISTED_UNAUTHENTICATED_ROUTES:
        assert key in live_keys, f"allowlist entry {key} does not match any live route"
    for key in WIFI_ROUTES_EXEMPT_FROM_FULL_GATING:
        assert key in live_keys, f"wifi allowlist entry {key} does not match any live route"


def test_wifi_prefix_constant_matches_the_real_wifi_router_prefix(app: FastAPI) -> None:
    # Guards WIFI_ACCESS_PREFIX itself against drifting from the real
    # router prefix (e.g. a future rename to /api/v1/network/) -- without
    # this, the wifi-specific branch above would silently stop matching any
    # route and every wifi route would need require_auth, which is not the
    # actual contract.
    wifi_routes = [route for route in _iter_api_routes(app) if route.path.startswith(WIFI_ACCESS_PREFIX)]
    assert wifi_routes, "sanity: expected at least one route under WIFI_ACCESS_PREFIX"


def test_wifi_router_declares_both_gates_at_the_router_level() -> None:
    # The most direct version of "every wifi route carries both gates":
    # both are declared once, on the router itself (api/wifi.py's
    # `APIRouter(..., dependencies=[...])`), and FastAPI/Starlette apply a
    # router's own dependencies to *every* route registered under it --
    # there is no per-route way to opt out. Checking the router's own
    # `.dependencies` directly is therefore not just a proxy for "every
    # route has both" (what the per-route walk above already re-derives);
    # it is the actual mechanism guaranteeing that, asserted at its source.
    router_dependency_calls = {dep.dependency for dep in wifi_api.router.dependencies}
    assert require_wifi_access in router_dependency_calls
    assert require_full_session in router_dependency_calls


def test_openapi_and_docs_mounts_are_not_under_api_prefix(tmp_path: Path) -> None:
    # Built against a real, valid frontend build layout with docs enabled
    # -- not the conftest `app` fixture, whose default settings
    # (enable_docs=False, a nonexistent static_dir) mean /docs, /redoc,
    # /openapi.json, and the frontend-assets mount are never actually
    # registered at all. Without this, the loop below iterates zero
    # matching entries and the test passes vacuously, never having checked
    # anything.
    static_dir = _built_static_dir(tmp_path)
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=static_dir, enable_docs=True)
    app = create_app(settings)

    checked = 0
    for entry in app.routes:
        path = getattr(entry, "path", None)
        if path is None:
            continue
        if path in {"/openapi.json", "/docs", "/redoc"}:
            checked += 1
            assert not path.startswith(API_PREFIX)
    assert checked == 3, "sanity: expected /openapi.json, /docs, and /redoc to all be mounted"


def test_static_asset_mount_is_not_under_api_prefix(tmp_path: Path) -> None:
    static_dir = _built_static_dir(tmp_path)
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=static_dir)
    app = create_app(settings)

    checked = 0
    for entry in app.routes:
        name = getattr(entry, "name", None)
        path = getattr(entry, "path", None)
        if name == "frontend-assets" and path is not None:
            checked += 1
            assert not path.startswith(API_PREFIX)
    assert checked == 1, "sanity: expected the frontend-assets mount to actually be registered"


def _built_static_dir(tmp_path: Path) -> Path:
    """A minimal fake `make build` output: enough for `_mount_frontend` to mount it.

    Mirrors ``test_app.py``'s helper of the same name -- kept local rather
    than imported across test modules (pytest test modules are not meant to
    import from one another).
    """
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html><title>Palmimo DevKit</title>", encoding="utf-8")
    (static_dir / "assets" / "index.js").write_text("// built asset\n", encoding="utf-8")
    return static_dir
