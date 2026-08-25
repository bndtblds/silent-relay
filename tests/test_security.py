from app.security.core import (
    CryptoError, FieldCipher, fingerprint, generate_token, hash_password,
    hash_pin, keyed_hash, verify_password, verify_pin,
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
    for trivial in ("aaaaaaaaaaaa", "Password123456!", "qwertz-qwertz"):
        try:
            hash_password(trivial)
        except ValueError:
            pass
        else:
            raise AssertionError(f"trivial password accepted: {trivial}")


def test_trusted_person_pin_is_argon2id_and_rejects_obvious_values():
    stored = hash_pin("472915")
    assert stored.startswith("$argon2id$")
    assert verify_pin(stored, "472915")
    assert not verify_pin(stored, "472916")
    for invalid in ("123456", "111111", "121212", "123123", "112233", "12345", "abcdef"):
        try:
            hash_pin(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe PIN accepted: {invalid}")


def test_authenticated_field_encryption(cipher):
    encrypted = cipher.encrypt("private@example.org")
    assert b"private@example.org" not in encrypted
    assert cipher.decrypt(encrypted) == "private@example.org"


def test_fingerprint_is_normalized(settings):
    assert fingerprint(" Test@Example.org ", settings.fingerprint_hmac_key) == fingerprint(
        "test@example.org", settings.fingerprint_hmac_key
    )
