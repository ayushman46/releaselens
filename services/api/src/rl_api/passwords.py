"""Password hashing (Argon2id) and the password policy.

**Hashing.** ``argon2-cffi`` with Argon2id. Defaults follow RFC 9106 §4's second recommended
option (t=3, m=64 MiB, p=4), configurable through ``RL_ARGON2_*``. Hashing runs in a worker
thread so it never blocks the event loop, and a process-wide semaphore caps how many run at
once (each one holds ``m`` of memory; the API container has a 384 MiB limit). When the
parameters change, the next successful login re-hashes the password with the new ones.

**Policy** (no network calls): at least 12 characters, at most 256; not one of a list of
very common passwords, nor such a word decorated with digits or symbols ("Password123!");
not mostly one repeated character; not a straight keyboard or alphabet run; not containing
the e-mail's local part or the product name. The reasons are returned so the UI can say
what to fix.
"""

from __future__ import annotations

import asyncio
import re
import threading
from typing import Final

from argon2 import PasswordHasher as _Argon2
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH: Final = 12
MAX_LENGTH: Final = 256

# Frequent entries of public breach corpora (top lists), lower-cased. Long ones are matched
# exactly; short ones are matched as the "core" of a decorated password.
COMMON_PASSWORDS: Final = frozenset(
    {
        "123456789012",
        "1234567890123",
        "12345678901234",
        "123123123123",
        "111111111111",
        "000000000000",
        "qwertyuiopas",
        "qwertyuiop123",
        "1q2w3e4r5t6y",
        "1qaz2wsx3edc",
        "passwordpassword",
        "password1234",
        "password12345",
        "iloveyou1234",
        "abcdefghijkl",
        "abcd1234abcd",
        "qwerty123456",
        "asdfghjkl123",
        "zaq12wsxcde3",
        "changeme1234",
        "letmeinletmein",
        "welcome12345",
        "administrator",
        "trustno1trustno1",
    }
)
COMMON_WORDS: Final = frozenset(
    {
        "password",
        "passw0rd",
        "qwerty",
        "qwertyuiop",
        "letmein",
        "welcome",
        "iloveyou",
        "admin",
        "administrator",
        "monkey",
        "dragon",
        "football",
        "baseball",
        "basketball",
        "sunshine",
        "princess",
        "trustno",
        "master",
        "shadow",
        "superman",
        "batman",
        "starwars",
        "whatever",
        "freedom",
        "computer",
        "internet",
        "changeme",
        "secret",
        "login",
        "default",
        "summer",
        "winter",
        "spring",
        "autumn",
        "january",
        "december",
        "abc",
        "abcdef",
        "asdf",
        "asdfgh",
        "zxcvbn",
        "hello",
        "charlie",
        "michael",
        "jennifer",
        "pokemon",
        "minecraft",
    }
)
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
_KEYBOARD_ROWS: Final = (
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
    "1234567890",
    "abcdefghijklmnopqrstuvwxyz",
)
_EDGE = re.compile(r"^[\W\d_]+|[\W\d_]+$")


class WeakPasswordError(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _is_run(text: str) -> bool:
    """A straight run along one keyboard row or the alphabet, forwards or backwards,
    possibly wrapping (``"1234567890123"``)."""
    if len(text) < 4:
        return False
    for row in _KEYBOARD_ROWS:
        for seq in (row, row[::-1]):
            doubled = seq * (len(text) // len(seq) + 2)
            if text in doubled:
                return True
    return False


def password_problems(password: str, *, email: str | None = None) -> list[str]:
    reasons: list[str] = []
    if len(password) < MIN_LENGTH:
        reasons.append(f"must be at least {MIN_LENGTH} characters")
    if len(password) > MAX_LENGTH:
        reasons.append(f"must be at most {MAX_LENGTH} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        reasons.append("must not contain control characters")
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS:
        reasons.append("is a commonly used password")
    else:
        # Strip the decoration first ("Password123!"), then undo leetspeak ("p@ssw0rd").
        stripped = _EDGE.sub("", lowered)
        core = stripped.translate(_LEET)
        if stripped in COMMON_WORDS or core in COMMON_WORDS or core in COMMON_PASSWORDS:
            reasons.append("is a common word with digits or symbols added")
    if len(set(lowered)) <= 3:
        reasons.append("uses too few different characters")
    if _is_run(lowered):
        reasons.append("is a keyboard or alphabet sequence")
    if "releaselens" in lowered:
        reasons.append("must not contain the product name")
    if email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 4 and local in lowered:
            reasons.append("must not contain your e-mail address")
    return reasons


def check_password(password: str, *, email: str | None = None) -> None:
    reasons = password_problems(password, email=email)
    if reasons:
        raise WeakPasswordError(reasons)


class PasswordHasher:
    def __init__(
        self, *, time_cost: int, memory_kib: int, parallelism: int, concurrency: int = 2
    ) -> None:
        self._hasher = _Argon2(
            time_cost=time_cost,
            memory_cost=memory_kib,
            parallelism=parallelism,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        # A thread semaphore, not an asyncio one: the work runs in threads, and this works
        # whichever event loop the request runs on.
        self._slots = threading.BoundedSemaphore(concurrency)
        # Verified against when the account does not exist, so that path costs the same.
        self._dummy_hash = self._hasher.hash("not-a-real-password-just-equal-timing")

    def _guarded_hash(self, password: str) -> str:
        with self._slots:
            return self._hasher.hash(password)

    def _guarded_verify(self, encoded: str, password: str) -> bool:
        with self._slots:
            try:
                return self._hasher.verify(encoded, password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                return False

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._guarded_hash, password)

    async def verify(self, encoded: str | None, password: str) -> bool:
        """Constant-ish time whether or not ``encoded`` exists (``None``: OAuth-only user
        or unknown account)."""
        if encoded is None:
            await asyncio.to_thread(self._guarded_verify, self._dummy_hash, password)
            return False
        return await asyncio.to_thread(self._guarded_verify, encoded, password)

    def needs_rehash(self, encoded: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(encoded)
        except (InvalidHashError, ValueError):
            return True
