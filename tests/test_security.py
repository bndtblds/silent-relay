from app.security.core import (
    CryptoError, FieldCipher, fingerprint, generate_token, hash_password, keyed_hash, verify_password,
)


def test_tokens_are_random_and_keyed(settings):
    first, second = generate_token(), generate_token()
    assert first != second
    assert len(first) >= 43
    assert keyed_hash(first, settings.token_hmac_key) != first
    assert keyed_hash(first, settings.token_hmac_key) != keyed_hash(second, settings.token_hmac_key)


def test_password_argon2id():
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("$argon2id$")
    assert verify_password(stored, "correct horse battery staple")
    assert not verify_password(stored, "wrong password")


def test_password_limits():
    try:
        hash_password("too short")
    except ValueError:
        pass
    else:
        raise AssertionError("short password accepted")


def test_authenticated_field_encryption(cipher):
    encrypted = cipher.encrypt("private@example.org")
    assert b"private@example.org" not in encrypted
    assert cipher.decrypt(encrypted) == "private@example.org"


def test_fingerprint_is_normalized(settings):
    assert fingerprint(" Test@Example.org ", settings.fingerprint_hmac_key) == fingerprint(
        "test@example.org", settings.fingerprint_hmac_key
    )
