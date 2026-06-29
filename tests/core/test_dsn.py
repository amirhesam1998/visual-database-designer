"""Unit tests for container-aware DSN host rewriting (import-fixes milestone §4)."""

from __future__ import annotations

from app.core import dsn as core_dsn


def test_rewrites_localhost_when_in_container():
    out, changed = core_dsn.rewrite_host_for_container(
        "mysql://root:pw@localhost:3306/db", force=True)
    assert changed is True
    assert out == "mysql://root:pw@host.docker.internal:3306/db"


def test_rewrites_127_0_0_1_when_in_container():
    out, changed = core_dsn.rewrite_host_for_container(
        "postgresql://u:p@127.0.0.1:5432/db", force=True)
    assert changed is True
    assert out == "postgresql://u:p@host.docker.internal:5432/db"


def test_preserves_password_with_at_sign():
    # The exact case the user hit: a password containing '@' must survive the rewrite untouched.
    out, changed = core_dsn.rewrite_host_for_container(
        "mysql://root:@Amir764740@localhost:3306/hosh_system", force=True)
    assert changed is True
    assert out == "mysql://root:@Amir764740@host.docker.internal:3306/hosh_system"


def test_no_rewrite_for_remote_host():
    dsn = "mysql://root:pw@db.example.com:3306/db"
    out, changed = core_dsn.rewrite_host_for_container(dsn, force=True)
    assert changed is False and out == dsn


def test_no_rewrite_when_not_in_container():
    dsn = "mysql://root:pw@localhost:3306/db"
    out, changed = core_dsn.rewrite_host_for_container(dsn, force=False)
    assert changed is False and out == dsn


def test_no_port_is_fine():
    out, changed = core_dsn.rewrite_host_for_container("mysql://root@localhost/db", force=True)
    assert changed is True
    assert out == "mysql://root@host.docker.internal/db"


def test_connection_hint_only_for_local_in_container(monkeypatch):
    monkeypatch.setenv("VDB_IN_CONTAINER", "1")
    assert core_dsn.connection_hint("mysql://root:pw@localhost:3306/db") is not None
    assert core_dsn.connection_hint("mysql://root:pw@db.example.com:3306/db") is None
    monkeypatch.setenv("VDB_IN_CONTAINER", "0")
    assert core_dsn.connection_hint("mysql://root:pw@localhost:3306/db") is None


def test_in_container_env_override(monkeypatch):
    monkeypatch.setenv("VDB_IN_CONTAINER", "1")
    assert core_dsn.in_container() is True
    monkeypatch.setenv("VDB_IN_CONTAINER", "0")
    assert core_dsn.in_container() is False
