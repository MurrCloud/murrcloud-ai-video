import sys

import pytest
from app import cli
from app.models import AuditEvent, User
from app.security import verify_password
from conftest import PASSWORD
from sqlalchemy import func, select


def provision_command(api, monkeypatch, *, email, passwords, role="admin"):
    replies = iter(passwords)
    engine_calls = []

    def engine(url):
        engine_calls.append(url)
        return api.app.state.engine

    monkeypatch.setattr(cli, "get_settings", lambda: api.settings)
    monkeypatch.setattr(cli, "make_engine", engine)
    # The CLI owns its engine, but this unit test injects the fixture's engine.
    # Preserve its in-memory database until assertions and fixture teardown.
    monkeypatch.setattr(api.app.state.engine, "dispose", lambda: None)
    monkeypatch.setattr(cli, "getpass", lambda _prompt: next(replies))
    monkeypatch.setattr(
        sys, "argv", ["video-studio", "create-user", "--email", email, "--role", role]
    )
    return engine_calls


def test_cli_provisions_admin_with_argon2_and_uuid_audit(api, monkeypatch, capsys):
    calls = provision_command(
        api, monkeypatch, email="NEW.ADMIN@example.com", passwords=[PASSWORD, PASSWORD]
    )
    cli.main()
    assert calls == [api.settings.database_url]
    with api.sessions() as db:
        user = db.scalar(select(User).where(User.email == "new.admin@example.com"))
        assert user is not None
        assert user.role == "admin" and user.active
        assert user.password_hash.startswith("$argon2id$")
        assert verify_password(PASSWORD, user.password_hash)
        event = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "user.provisioned"))
        assert event.actor_user_id == user.id
        assert event.entity_id == user.id
        assert event.entity_type == "user"
        assert event.details == {"role": "admin"}
    output = capsys.readouterr()
    assert "User provisioned" in output.out
    assert PASSWORD not in output.out + output.err


def test_cli_rejects_existing_user_without_duplicate_or_audit(api, monkeypatch, capsys):
    provision_command(api, monkeypatch, email="EDITOR@example.com", passwords=[PASSWORD, PASSWORD])
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 2
    with api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(User)) == len(api.user_ids)
        assert db.scalar(select(AuditEvent)) is None
    output = capsys.readouterr()
    assert "User already exists" in output.err
    assert PASSWORD not in output.out + output.err


@pytest.mark.parametrize(
    "passwords,message",
    [
        ([PASSWORD, "different-password"], "Passwords did not match"),
        (["short", "short"], "Password must contain 12–256 characters"),
        (["x" * 257, "x" * 257], "Password must contain 12–256 characters"),
    ],
)
def test_cli_rejects_invalid_passwords_before_database_access(
    api, monkeypatch, capsys, passwords, message
):
    calls = provision_command(
        api, monkeypatch, email="unprovisioned@example.com", passwords=passwords
    )
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 2
    assert calls == []
    with api.sessions() as db:
        assert db.scalar(select(func.count()).select_from(User)) == len(api.user_ids)
        assert db.scalar(select(AuditEvent)) is None
    output = capsys.readouterr()
    assert message in output.err
    assert PASSWORD not in output.out + output.err
