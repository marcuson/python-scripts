import json
from dataclasses import dataclass
from functools import cache
from importlib.metadata import (
    PackageNotFoundError,
    distribution,
    metadata,
)
from pathlib import Path


@dataclass
class PkgMeta:
    name: str
    is_installed: bool
    is_local_exec: bool
    dist_uri: str


@cache
def _get_package_name() -> str:
    current_pkg = __name__.split(".")
    return current_pkg[0]


@cache
def _is_pkg_installed() -> bool:
    try:
        metadata(_get_package_name())
        return True
    except ImportError:
        return False


@cache
def _get_pkg_direct_url() -> dict[str, str] | None:
    """Get package direct_url.json content as dict. Return None if not found or local exec."""

    # If None, is run as local script
    if not _is_pkg_installed():
        return None

    try:
        current_pkg = _get_package_name()
        pkg_dist = distribution(current_pkg)
    except PackageNotFoundError:
        # If running as a raw uninstalled script, it's likely local development
        # Note: this is just a security measure, this branch should not be hit
        return None

    direct_url_json = pkg_dist.read_text("direct_url.json")
    if direct_url_json is None:
        return None

    return json.loads(direct_url_json)


def _is_local_exec() -> bool:
    """Check if installer exec has been launched locally or remotely via 'uvx https://...'"""
    direct_url = _get_pkg_direct_url()
    if direct_url is None:
        return True

    dist_url = direct_url.get("url", "")
    return "file://" in dist_url


@cache
def get_pkg_metadata() -> PkgMeta:
    direct_url_json = _get_pkg_direct_url()
    dist_uri = (
        direct_url_json.get("url", "")
        if direct_url_json is not None
        else Path(__file__).parent.parent.as_uri()
    )

    res = PkgMeta(
        dist_uri=dist_uri,
        is_installed=_is_pkg_installed(),
        is_local_exec=_is_local_exec(),
        name=_get_package_name(),
    )
    return res
