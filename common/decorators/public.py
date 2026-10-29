from collections.abc import Callable
from typing import Any, TypeVar

_PUBLIC_ATTR = "__auth_public__"

EndpointT = TypeVar("EndpointT", bound=Callable[..., Any])


def public(endpoint: EndpointT) -> EndpointT:
    """Mark a route endpoint as reachable without a token. Every other route requires one."""
    setattr(endpoint, _PUBLIC_ATTR, True)
    return endpoint


def is_public(endpoint: Any) -> bool:
    return getattr(endpoint, _PUBLIC_ATTR, False)
