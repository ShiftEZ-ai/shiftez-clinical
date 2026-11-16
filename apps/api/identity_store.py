"""Sign-in accounts in the environment's Authelia users file (authentication_backend.file, watch: true).

The API is the only writer. Each change is read-modify-write under an exclusive lock file next to the users file,
written to a temporary file in the same directory, flushed to disk and moved over the original with os.replace, so
Authelia (and a crash) only ever sees the old or the new complete file. Users and fields this module does not manage
are kept as they are.

The file is written as JSON, which is valid YAML: Authelia reads it unchanged and no YAML library is needed here. A file
that is not in that form (hand-edited YAML) is refused with a clear message instead of being guessed at; convert it
once with scripts/local-deploy/identity_volume.py.

Passwords are hashed with argon2id using Authelia's default parameters (the parameters travel in the hash, so Authelia
verifies them as written). Plain passwords never leave the calling function and are never logged.
"""
import json
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
# Authelia's argon2id defaults (authentication_backend.password_hashing.argon2): iterations 3, memory 64 MiB,
# parallelism 4, key 32 bytes, salt 16 bytes.
HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID)
MAX_FILE_BYTES = 5_000_000
_DUMMY = HASHER.hash(secrets.token_urlsafe(16))  # unknown names cost the same as known ones


class IdentityStoreError(RuntimeError):
    """The users file could not be read or written; the message is safe to show (no secrets)."""


class IdentityStore(Protocol):
    def get(self, username: str) -> dict | None: ...
    def has_credential(self, username: str) -> bool: ...
    def create(self, username: str, display_name: str, email: str) -> None: ...
    def set_password(self, username: str, password: str) -> None: ...
    def set_disabled(self, username: str, disabled: bool) -> None: ...
    def verify(self, username: str, password: str) -> bool: ...
    def verify_credential(self, username: str, password: str) -> str | None: ...
    def credential_matches(self, username: str, credential: str) -> bool: ...
    def remove(self, username: str) -> None: ...


def hash_password(password: str) -> str:
    return HASHER.hash(password)


@contextmanager
def _locked(path: Path):
    """Exclusive lock across processes (and threads: each call opens its own descriptor)."""
    with open(path.with_name(path.name + ".lock"), "a+b") as handle:
        if os.name == "nt":  # development machines only; the supported deployment is Linux
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def validate(data) -> dict:
    """The shape Authelia's file backend accepts (as far as this module relies on it)."""
    if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
        raise IdentityStoreError("The users file must contain a 'users' mapping")
    for name, user in data["users"].items():
        if not isinstance(user, dict) or not isinstance(user.get("password"), str) or not isinstance(user.get("displayname"), str):
            raise IdentityStoreError(f"Sign-in user {name!r} lacks a display name or password hash")
        if "disabled" in user and not isinstance(user["disabled"], bool):
            raise IdentityStoreError(f"Sign-in user {name!r} has an invalid 'disabled' value")
    return data


class AutheliaUsersFile:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            raw = self.path.read_bytes()
        except OSError:
            raise IdentityStoreError("The sign-in users file cannot be read; check ROTA_IDENTITY_USERS_FILE and its mount") from None
        if len(raw) > MAX_FILE_BYTES:
            raise IdentityStoreError("The sign-in users file is unexpectedly large")
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, ValueError):
            raise IdentityStoreError("The sign-in users file is not in the form Rota writes (JSON-style YAML); convert it with "
                                     "scripts/local-deploy/identity_volume.py") from None
        return validate(data)

    def _write(self, data: dict) -> None:
        text = json.dumps(validate(data), indent=2, ensure_ascii=False) + "\n"
        json.loads(text)  # what Authelia will read back parses
        mode = self.path.stat().st_mode & 0o777 if self.path.exists() else 0o600
        fd, temp = tempfile.mkstemp(prefix=".users-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp, mode)
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    def _change(self, apply) -> None:
        with _locked(self.path):
            data = self._read()
            apply(data["users"])
            self._write(data)

    def get(self, username: str) -> dict | None:
        user = self._read()["users"].get(username)
        return None if user is None else {k: v for k, v in user.items() if k != "password"}

    def has_credential(self, username: str) -> bool:
        """Check recovery eligibility without exposing a hash or verifying an unknown password."""
        user = self._read()["users"].get(username)
        try:
            return bool(user and not user.get("disabled") and extract_parameters(user["password"]).type == Type.ID)
        except InvalidHashError:
            return False

    def create(self, username: str, display_name: str, email: str) -> None:
        """A new user that cannot sign in yet: disabled, with the hash of a random secret nobody knows."""
        if not USERNAME.fullmatch(username):
            raise IdentityStoreError("Sign-in names use 2-63 lowercase letters, digits, '.', '_' or '-'")
        digest = hash_password(secrets.token_urlsafe(32))

        def apply(users):
            existing = users.get(username)
            # shortcut: a retried transaction may find its own pending user from the first attempt; the same pending
            # (disabled) user with the same name and email is that one, anything else is someone else's account.
            if existing is not None and not (existing.get("disabled") is True and existing.get("email") == email
                                             and existing.get("displayname") == display_name):
                raise IdentityStoreError("This sign-in name is already taken")
            users[username] = {"displayname": display_name, "password": digest, "email": email, "groups": [], "disabled": True}
        self._change(apply)

    def set_password(self, username: str, password: str) -> None:
        """Store a new password hash and enable the user."""
        digest = hash_password(password)

        def apply(users):
            if username not in users:
                raise IdentityStoreError("This sign-in no longer exists in the sign-in service")
            users[username]["password"] = digest
            users[username]["disabled"] = False
        self._change(apply)

    def set_disabled(self, username: str, disabled: bool) -> None:
        def apply(users):
            if username not in users:
                raise IdentityStoreError("This sign-in no longer exists in the sign-in service")
            users[username]["disabled"] = disabled
        self._change(apply)

    def remove(self, username: str) -> None:
        """Delete the sign-in entry (password hash included); a missing one is already gone."""
        self._change(lambda users: users.pop(username, None))

    def verify(self, username: str, password: str) -> bool:
        return self.verify_credential(username, password) is not None

    def credential_matches(self, username: str, credential: str) -> bool:
        user = self._read()["users"].get(username)
        return bool(user and user["password"] == credential and not user.get("disabled", False))

    def verify_credential(self, username: str, password: str) -> str | None:
        """Verify outside the database lock; return the exact credential to recheck before issuing a session."""
        for attempt in range(2):
            user = self._read()["users"].get(username)
            stored = user["password"] if user else _DUMMY
            try:
                parameters = extract_parameters(stored)
                usable = parameters.type == Type.ID
            except InvalidHashError:
                usable = False
            # Corrupt/non-Argon2id entries still pay a full verify. Cost parameters in this trusted file remain compatible.
            stored = stored if usable else _DUMMY
            try:
                HASHER.verify(stored, password)
            except VerifyMismatchError:
                return None
            except (VerificationError, InvalidHashError):
                try:
                    HASHER.verify(_DUMMY, password)
                except (VerificationError, InvalidHashError):
                    pass
                return None
            valid = usable and user is not None and not user.get("disabled", False)
            # A mismatch alone is not an upgrade: retain any stronger or mixed-strength parameter set.
            target = extract_parameters(_DUMMY)
            upgrade = usable and all(getattr(target, field) >= getattr(parameters, field) for field in
                                     ("version", "time_cost", "memory_cost", "parallelism", "hash_len", "salt_len"))
            if valid and upgrade and HASHER.check_needs_rehash(stored):
                digest = hash_password(password)

                def apply(users):
                    nonlocal valid
                    current = users.get(username)
                    # Never overwrite a concurrent password reset, disable, removal or rehash.
                    valid = bool(current and current["password"] == stored and not current.get("disabled", False))
                    if valid:
                        current["password"] = digest
                try:
                    self._change(apply)
                except OSError:
                    raise IdentityStoreError("The sign-in users file cannot be updated; check its writable mount") from None
                # A competing login may have upgraded the same password; verify its new hash once outside the lock.
                if not valid and attempt == 0:
                    continue
                stored = digest
            return stored if valid and self.credential_matches(username, stored) else None


def store_for(settings) -> IdentityStore | None:
    return AutheliaUsersFile(settings.identity_users_file) if settings.identity_users_file else None
