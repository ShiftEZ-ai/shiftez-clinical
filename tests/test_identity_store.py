"""The Authelia users file the API manages: atomic, locked, preserving, Authelia-compatible argon2id."""
import json
import secrets
import threading
import pytest
from argon2 import PasswordHasher
from apps.api.identity_store import AutheliaUsersFile, IdentityStoreError

KEPT = {"displayname": "Kept (test)", "password": "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaGhhc2g",
        "email": "kept@example.test", "groups": ["ops"], "given_name": "Kept"}


@pytest.fixture
def users(tmp_path):
    path = tmp_path / "users.yml"
    path.write_text(json.dumps({"users": {"kept": KEPT}}), encoding="utf-8")
    return AutheliaUsersFile(path)


def test_create_is_disabled_and_password_enables_with_authelia_argon2id(users):
    users.create("new.person", "New Person (test)", "new@example.test")
    entry = users.get("new.person")
    assert entry["disabled"] is True and "password" not in entry
    assert not users.verify("new.person", "anything-at-all-123")
    users.set_password("new.person", "a long passphrase 12")
    raw = json.loads(users.path.read_text(encoding="utf-8"))["users"]["new.person"]
    assert raw["disabled"] is False
    assert raw["password"].startswith("$argon2id$v=19$m=65536,t=3,p=4$")  # Authelia's defaults
    assert PasswordHasher().verify(raw["password"], "a long passphrase 12")
    assert users.verify("new.person", "a long passphrase 12")
    assert not users.verify("new.person", "a long passphrase 13")
    assert not users.verify("nobody", "a long passphrase 12")


def test_password_hasher_meets_owasp_argon2id_floor():
    from argon2 import Type
    from apps.api.identity_store import HASHER

    assert HASHER.type == Type.ID
    assert HASHER.memory_cost >= 19 * 1024  # OWASP Password Storage Cheat Sheet, Argon2id minimum.
    assert HASHER.time_cost >= 2
    assert HASHER.parallelism >= 1
    assert HASHER.hash_len >= 16 and HASHER.salt_len >= 16


def test_other_users_and_unknown_fields_are_preserved(users):
    users.create("new.person", "New Person (test)", "new@example.test")
    users.set_disabled("new.person", False)
    assert json.loads(users.path.read_text(encoding="utf-8"))["users"]["kept"] == KEPT


def test_disabled_user_is_refused_after_password_verification(users, monkeypatch):
    from apps.api import identity_store
    users.create("disabled.person", "Disabled Person (test)", "disabled@example.test")
    users.set_password("disabled.person", "a long passphrase 12")
    users.set_disabled("disabled.person", True)
    calls = []
    original = type(identity_store.HASHER).verify

    def verify(hasher, stored, password):
        if hasher is identity_store.HASHER:
            calls.append(stored)
        return original(hasher, stored, password)

    monkeypatch.setattr(type(identity_store.HASHER), "verify", verify)
    assert not users.verify("disabled.person", "a long passphrase 12")
    assert len(calls) == 1
    users.set_disabled("disabled.person", False)
    assert users.verify("disabled.person", "a long passphrase 12")


def test_taken_names_and_bad_names_are_refused(users):
    with pytest.raises(IdentityStoreError):
        users.create("kept", "Someone else", "else@example.test")
    with pytest.raises(IdentityStoreError):
        users.create("Bad Name", "Someone", "s@example.test")
    users.create("pending", "Pending (test)", "p@example.test")
    users.create("pending", "Pending (test)", "p@example.test")  # the same pending user again (a retried transaction)
    with pytest.raises(IdentityStoreError):
        users.set_password("nobody", "a long passphrase 12")


def test_a_failed_write_leaves_the_original_file_and_no_temporary_files(users, monkeypatch):
    before = users.path.read_bytes()
    import apps.api.identity_store as module

    def broken(*_):
        raise OSError("disk full")
    monkeypatch.setattr(module.os, "replace", broken)
    with pytest.raises(OSError):
        users.create("new.person", "New Person (test)", "new@example.test")
    assert users.path.read_bytes() == before
    assert sorted(p.name for p in users.path.parent.iterdir()) == ["users.yml", "users.yml.lock"]


def test_unreadable_or_invalid_files_are_refused_not_guessed(tmp_path):
    path = tmp_path / "users.yml"
    path.write_text("users:\n  someone:\n    password: x\n", encoding="utf-8")
    with pytest.raises(IdentityStoreError, match="convert"):
        AutheliaUsersFile(path).get("someone")
    path.write_text(json.dumps({"users": {"x": {"displayname": "X"}}}), encoding="utf-8")
    with pytest.raises(IdentityStoreError):
        AutheliaUsersFile(path).get("x")
    with pytest.raises(IdentityStoreError):
        AutheliaUsersFile(tmp_path / "missing.yml").get("x")


def test_concurrent_writers_never_lose_a_change(users):
    errors = []

    def create(i):
        try:
            AutheliaUsersFile(users.path).create(f"person{i:02d}", f"Person {i} (test)", f"p{i}@example.test")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
    threads = [threading.Thread(target=create, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    names = set(json.loads(users.path.read_text(encoding="utf-8"))["users"])
    assert names == {"kept", *(f"person{i:02d}" for i in range(12))}


@pytest.mark.parametrize("stored", ["corrupt hash", "$argon2id$v=19$m=65536,t=3,p=4$%%%%$%%%%"])
def test_corrupt_hash_still_pays_full_dummy_verification(users, monkeypatch, stored):
    from apps.api import identity_store
    data = json.loads(users.path.read_text(encoding="utf-8"))
    data["users"]["kept"]["password"] = stored
    users.path.write_text(json.dumps(data), encoding="utf-8")
    verified = []
    original = type(identity_store.HASHER).verify
    def record(hasher, encoded, password):
        verified.append(encoded)
        return original(hasher, encoded, password)
    monkeypatch.setattr(type(identity_store.HASHER), "verify", record)
    assert not users.verify("kept", secrets.token_urlsafe(30))
    assert verified[-1] == identity_store._DUMMY


def test_successful_verification_upgrades_argon2_parameters_atomically(users):
    from apps.api.identity_store import HASHER
    password = secrets.token_urlsafe(30)
    data = json.loads(users.path.read_text(encoding="utf-8"))
    data["users"]["kept"]["password"] = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(password)
    data["users"]["other"] = {**KEPT, "displayname": "Other (test)"}
    users.path.write_text(json.dumps(data), encoding="utf-8")
    assert users.verify("kept", password)
    updated = json.loads(users.path.read_text(encoding="utf-8"))
    stored = updated["users"]["kept"]["password"]
    assert stored != data["users"]["kept"]["password"] and not HASHER.check_needs_rehash(stored)
    assert HASHER.verify(stored, password)
    updated["users"]["kept"]["password"] = data["users"]["kept"]["password"]
    assert updated == data


def test_current_hash_is_verified_without_rewriting_the_file(users, monkeypatch):
    password = secrets.token_urlsafe(30)
    users.set_password("kept", password)
    before = users.path.read_bytes()
    monkeypatch.setattr(users, "_change", lambda *_: pytest.fail("Current parameters must not rewrite the users file"))
    assert users.verify("kept", password)
    assert users.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["mismatch", "disabled", "unknown", "malformed"])
def test_unsuccessful_verification_never_rehashes(users, monkeypatch, failure):
    password = secrets.token_urlsafe(30)
    data = json.loads(users.path.read_text(encoding="utf-8"))
    data["users"]["kept"]["password"] = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(password)
    if failure == "disabled":
        data["users"]["kept"]["disabled"] = True
    elif failure == "malformed":
        data["users"]["kept"]["password"] = "corrupt hash"
    users.path.write_text(json.dumps(data), encoding="utf-8")
    before = users.path.read_bytes()
    monkeypatch.setattr(users, "_change", lambda *_: pytest.fail("Failed verification must not rewrite the users file"))
    assert not users.verify("unknown" if failure == "unknown" else "kept",
                            secrets.token_urlsafe(30) if failure == "mismatch" else password)
    assert users.path.read_bytes() == before


@pytest.mark.parametrize("change", ["reset", "disable", "remove"])
def test_rehash_cannot_overwrite_a_concurrent_reset_disable_or_removal(users, monkeypatch, change):
    from apps.api.identity_store import HASHER
    password, replacement = secrets.token_urlsafe(30), secrets.token_urlsafe(30)
    data = json.loads(users.path.read_text(encoding="utf-8"))
    old_hash = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(password)
    data["users"]["kept"]["password"] = old_hash
    users.path.write_text(json.dumps(data), encoding="utf-8")
    replacement_hash = HASHER.hash(replacement)
    original = users._change

    def concurrent(apply):
        def change_first(current):
            if change == "remove":
                current.pop("kept")
            else:
                current["kept"].update({"password": replacement_hash} if change == "reset" else {"disabled": True})
        original(change_first)
        original(apply)
    monkeypatch.setattr(users, "_change", concurrent)
    assert not users.verify("kept", password)
    current = json.loads(users.path.read_text(encoding="utf-8"))["users"]
    if change == "remove":
        assert "kept" not in current
    else:
        assert current["kept"]["password"] == (replacement_hash if change == "reset" else old_hash)
        assert current["kept"].get("disabled", False) is (change == "disable")


def test_rehash_write_failure_has_a_safe_error_and_preserves_the_original(users, monkeypatch):
    password = secrets.token_urlsafe(30)
    data = json.loads(users.path.read_text(encoding="utf-8"))
    data["users"]["kept"]["password"] = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(password)
    users.path.write_text(json.dumps(data), encoding="utf-8")
    before = users.path.read_bytes()

    def broken(*_):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(users, "_write", broken)
    with pytest.raises(IdentityStoreError, match="cannot be updated") as error:
        users.verify("kept", password)
    assert password not in str(error.value) and "synthetic write failure" not in str(error.value)
    assert users.path.read_bytes() == before
