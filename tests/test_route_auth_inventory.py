"""Fail closed when an API route is added without an auth dependency."""
from fastapi.routing import APIRoute

from app.main import app


# Intentionally public: health/version checks, login and recovery flows,
# challenge-based MFA bootstrap, public configuration, basemaps, and bounded
# administrative-boundary lookups used before login by the AOI picker.
PUBLIC_ROUTES = {
    ("GET", "/"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/redoc"),
    ("GET", "/api/live"),
    ("GET", "/api/ready"),
    ("GET", "/api/version"),
    ("GET", "/api/health"),
    ("GET", "/api/basemaps"),
    ("GET", "/api/regions/provinces"),
    ("GET", "/api/regions/cities"),
    ("GET", "/api/regions/provinces/{code}/geometry"),
    ("GET", "/api/regions/provinces/{code}/cities"),
    ("GET", "/api/regions/cities/{code}/geometry"),
    ("GET", "/api/regions/districts"),
    ("GET", "/api/regions/villages"),
    ("GET", "/api/regions/geometry"),
    ("GET", "/api/regions/children-geometries"),
    ("GET", "/api/regions/islands"),
    ("GET", "/api/regions/islands/{island_name}/geometry"),
    ("GET", "/api/regions/indonesia/geometry"),
    ("GET", "/api/regions/global/countries"),
    ("GET", "/api/regions/global/{iso3}/{level}"),
    ("GET", "/api/regions/global/{iso3}/{level}/geometry"),
    ("POST", "/api/admin/auth/login"),
    ("POST", "/api/admin/auth/forgot-password"),
    ("POST", "/api/admin/auth/reset-password"),
    ("GET", "/api/admin/config/public"),
    ("POST", "/api/admin/auth/mfa/enrollment"),
    ("POST", "/api/admin/auth/mfa/verify"),
    ("GET", "/api/auth/config"),
    ("POST", "/api/auth/register"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/forgot-password"),
    ("POST", "/api/auth/reset-password"),
}


def _iter_api_routes(router):
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield route
        elif type(route).__name__ == "_IncludedRouter":
            yield from _iter_api_routes(route.include_context.included_router)


def _dependency_names(route: APIRoute) -> set[str]:
    names = set()

    def visit(dependant):
        for item in dependant.dependencies:
            if item.call is not None:
                names.add(getattr(item.call, "__name__", type(item.call).__name__))
            visit(item)

    visit(route.dependant)
    return names


def test_every_nonpublic_api_route_has_authentication_dependency():
    included = next(route for route in app.routes if type(route).__name__ == "_IncludedRouter")
    unauthenticated = []
    auth_dependencies = {
        "get_current_user",
        "get_current_admin",
        "get_current_admin_panel",
        "get_current_app_viewer",
        "get_current_disaster_viewer",
        "optional_user",
        "_dependency",  # require_permission / require_feature dependency
    }

    routes = [route for route in app.routes if isinstance(route, APIRoute)]
    routes.extend(_iter_api_routes(included.include_context.included_router))
    for route in routes:
        path = route.path if route.path.startswith("/") else "/" + route.path
        if included.include_context.prefix and path not in {"/", "/docs", "/redoc", "/openapi.json"}:
            path = included.include_context.prefix.rstrip("/") + path
        for method in route.methods or ():
            if (method, path) in PUBLIC_ROUTES:
                continue
            if not _dependency_names(route) & auth_dependencies:
                unauthenticated.append((method, path))

    assert unauthenticated == []
