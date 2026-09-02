"""Passwords are really hashed with bcrypt, and the version shim did not change that.

A shim that quiets a warning by making a library think a backend is present would be
considerably worse than the warning. These assert the properties that matter: the hash
is a real bcrypt hash at the configured cost, the right password verifies, and the wrong
one does not.
"""

from __future__ import annotations

from services.api.security import hash_password, verify_password


def test_a_hash_is_a_real_bcrypt_hash():
    """`$2b$` is bcrypt's own identifier, and `12` the cost.

    Checked rather than assumed: passlib falls back to other schemes when a backend is
    unusable, and a silently weaker hash is exactly what a warning about the bcrypt
    backend ought to make somebody check.
    """
    h = hash_password("a-password-worth-hashing")
    assert h.startswith("$2b$"), h[:12]
    cost = int(h.split("$")[2])
    assert cost >= 12, f"bcrypt cost dropped to {cost}"


def test_the_right_password_verifies_and_a_wrong_one_does_not():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("Correct horse battery staple", h) is False
    assert verify_password("", h) is False


def test_two_hashes_of_one_password_differ():
    """Salting. Identical hashes would mean the salt is fixed or absent."""
    a = hash_password("same input")
    b = hash_password("same input")
    assert a != b
    assert verify_password("same input", a)
    assert verify_password("same input", b)


def test_passlib_can_read_the_bcrypt_version():
    """The specific lookup that printed a traceback on every process start.

    passlib reads `bcrypt.__about__.__version__` only to log which backend it found.
    bcrypt 4.1 removed `__about__`, so the read raised, passlib trapped it, and every
    boot logged an AttributeError under "(trapped) error reading bcrypt version". A
    traceback that appears on every start is one an operator learns to scroll past,
    which is a habit worth not teaching them.
    """
    import bcrypt

    assert bcrypt.__about__.__version__  # type: ignore[attr-defined]
