from pathlib import Path
from types import SimpleNamespace

import pytest

from server.cli import _resolve_db_path, bootstrap_owner, cmd_list_users, reset_password
from server.persistence.database import Database
from server.core.users.base import TrustLevel
from server.auth.auth import AuthManager
from server.core.server import Server, BOOTSTRAP_WARNING_ENV

LOCALES_DIR = Path(__file__).resolve().parents[1] / "locales"


def test_resolve_db_path_defaults_to_server_database():
    resolved = _resolve_db_path(None)
    assert resolved.parts[-3:] == ("var", "server", "playpalace.db")


def test_reset_password_updates_existing_user(tmp_path):
    db_path = tmp_path / "playpalace.db"
    bootstrap_owner(
        db_path=str(db_path),
        username="admin",
        password="old-secret",
        quiet=True,
    )

    reset_password(
        db_path=str(db_path),
        username="admin",
        password="new-secret",
        quiet=True,
    )

    database = Database(db_path)
    database.connect()
    try:
        user = database.get_user("admin")
        assert user is not None
        auth = AuthManager(database)
        assert auth.verify_password("new-secret", user.password_hash)
        assert not auth.verify_password("old-secret", user.password_hash)
    finally:
        database.close()


def test_reset_password_requires_existing_database_and_account(tmp_path):
    missing_db = tmp_path / "missing.db"
    with pytest.raises(RuntimeError):
        reset_password(db_path=str(missing_db), username="admin", password="new", quiet=True)
    assert not missing_db.exists()

    db_path = tmp_path / "playpalace.db"
    bootstrap_owner(db_path=str(db_path), username="admin", password="old", quiet=True)
    with pytest.raises(RuntimeError):
        reset_password(db_path=str(db_path), username="ghost", password="new", quiet=True)


def test_cmd_list_users_reports_trust_levels(capsys, tmp_path):
    db_path = tmp_path / "playpalace.db"
    bootstrap_owner(db_path=str(db_path), username="admin", password="old", quiet=True)

    cmd_list_users(SimpleNamespace(db_path=str(db_path)))

    out = capsys.readouterr().out
    assert "admin: SERVER_OWNER" in out


def test_bootstrap_owner_creates_new_owner(tmp_path):
    db_path = tmp_path / "playpalace.db"

    bootstrap_owner(
        db_path=str(db_path),
        username="admin",
        password="secret-pass",
        locale="en",
        force=False,
        quiet=True,
    )

    database = Database(db_path)
    database.connect()
    try:
        user = database.get_user("admin")
        assert user is not None
        assert user.trust_level == TrustLevel.SERVER_OWNER
        assert user.approved

        auth = AuthManager(database)
        assert auth.verify_password("secret-pass", user.password_hash)
    finally:
        database.close()


def test_bootstrap_owner_requires_force_when_users_exist(tmp_path):
    db_path = tmp_path / "existing.db"
    database = Database(db_path)
    database.connect()
    try:
        auth = AuthManager(database)
        database.create_user(
            username="existing",
            password_hash=auth.hash_password("pw"),
            trust_level=TrustLevel.USER,
            approved=False,
        )
    finally:
        database.close()

    with pytest.raises(RuntimeError):
        bootstrap_owner(
            db_path=str(db_path),
            username="admin",
            password="secret",
            force=False,
            quiet=True,
        )


def test_bootstrap_owner_force_updates_existing_user(tmp_path):
    db_path = tmp_path / "override.db"
    database = Database(db_path)
    database.connect()
    try:
        auth = AuthManager(database)
        database.create_user(
            username="admin",
            password_hash=auth.hash_password("old"),
            trust_level=TrustLevel.USER,
            approved=False,
        )
    finally:
        database.close()

    bootstrap_owner(
        db_path=str(db_path),
        username="admin",
        password="new-secret",
        force=True,
        quiet=True,
    )

    database = Database(db_path)
    database.connect()
    try:
        user = database.get_user("admin")
        assert user.trust_level == TrustLevel.SERVER_OWNER
        assert user.approved
        auth = AuthManager(database)
        assert auth.verify_password("new-secret", user.password_hash)
    finally:
        database.close()


def test_warn_if_no_users_prints_message(capsys, tmp_path):
    server = Server(db_path=str(tmp_path / "db.sqlite"), locales_dir=LOCALES_DIR)
    server._db = SimpleNamespace(get_user_count=lambda: 0)
    server._warn_if_no_users()
    out = capsys.readouterr().out
    assert "bootstrap-owner" in out


def test_warn_if_no_users_respects_env(monkeypatch, capsys, tmp_path):
    server = Server(db_path=str(tmp_path / "db.sqlite"), locales_dir=LOCALES_DIR)
    server._db = SimpleNamespace(get_user_count=lambda: 0)
    monkeypatch.setenv(BOOTSTRAP_WARNING_ENV, "1")
    server._warn_if_no_users()
    out = capsys.readouterr().out
    assert out == ""
