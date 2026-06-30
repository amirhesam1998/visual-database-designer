"""DSN host smartness for containerised use (import-fixes milestone §4).

When the Designer runs **inside a Docker container**, a user-supplied ``localhost`` / ``127.0.0.1`` in a
connection string points at the *container*, not the user's machine — so a database on the host is
unreachable (``Connection refused``). Docker Desktop exposes the host as ``host.docker.internal``; this
module rewrites the DSN host accordingly, but **only when we are actually in a container** (so nothing
changes for a normal host install). It also builds a friendly hint for connection errors.

The rewrite preserves the rest of the DSN byte-for-byte — including passwords that contain ``@`` or
other characters — by reconstructing the authority from the raw (still-encoded) userinfo parts rather
than re-encoding them.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_CONTAINER_HOST = "host.docker.internal"


def in_container() -> bool:
    """True when running inside a container. ``VDB_IN_CONTAINER`` overrides detection (1/0)."""
    override = os.getenv("VDB_IN_CONTAINER")
    if override is not None:
        return override.strip() in {"1", "true", "yes"}
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as fh:
            return any(("docker" in line or "kubepods" in line) for line in fh)
    except OSError:
        return False


def rewrite_host_for_container(dsn: str, *, force: bool | None = None) -> tuple[str, bool]:
    """Rewrite a ``localhost``/``127.0.0.1`` DSN host to ``host.docker.internal`` when in a container.

    Returns ``(dsn, changed)``. ``force`` overrides the in-container check (used by tests / explicit
    opt-in). The password and every other component are preserved exactly.
    """
    use = in_container() if force is None else force
    if not use or not dsn:
        return dsn, False
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return dsn, False
    host = (parts.hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        return dsn, False

    # Reconstruct the authority, keeping the raw userinfo (urlsplit does not percent-decode it).
    userinfo = ""
    if parts.username is not None:
        userinfo = parts.username
        if parts.password is not None:
            userinfo += ":" + parts.password
        userinfo += "@"
    netloc = f"{userinfo}{_CONTAINER_HOST}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), True


def driver_for_dsn(dsn: str | None) -> str | None:
    """Infer the Core driver name from a DSN scheme (``mysql``/``mariadb`` → ``mysql``,
    ``postgres``/``postgresql`` → ``postgres``, ``sqlserver``/``mssql`` → ``sqlserver``). Returns
    ``None`` for an unknown/blank scheme so the caller can fall back to its default — this is how "the
    driver is determined from the connection" (multi-driver milestone §3) without the UI stating it."""
    if not dsn:
        return None
    try:
        scheme = (urlsplit(dsn).scheme or "").lower()
    except ValueError:
        return None
    if scheme.startswith(("mysql", "mariadb")):
        return "mysql"
    if scheme.startswith("postgres"):
        return "postgres"
    if scheme.startswith(("sqlserver", "mssql")):
        return "sqlserver"
    return None


def is_local_host(dsn: str) -> bool:
    """Whether the DSN targets a loopback host (used to tailor connection-error hints)."""
    try:
        return (urlsplit(dsn).hostname or "").lower() in _LOCAL_HOSTS
    except ValueError:
        return False


def connection_hint(dsn: str) -> str | None:
    """A human hint for a failed connection, when the cause is likely the container/localhost trap."""
    if in_container() and is_local_host(dsn):
        return ("This tool runs inside a container, where 'localhost' is the container itself. "
                "To reach a database on your own computer use 'host.docker.internal' as the host; "
                "for a remote server use its real IP/hostname.")
    return None
