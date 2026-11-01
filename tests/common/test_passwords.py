from common.auth import PasswordHasher


def test_hash_is_argon2id_and_salted():
    hasher = PasswordHasher()
    first = hasher.hash("correct horse battery staple")
    second = hasher.hash("correct horse battery staple")
    assert first.startswith("$argon2id$")
    assert first != second


def test_verify_accepts_correct_and_rejects_wrong_password():
    hasher = PasswordHasher()
    password_hash = hasher.hash("correct horse battery staple")
    assert hasher.verify(password_hash, "correct horse battery staple")
    assert not hasher.verify(password_hash, "wrong password")


def test_verify_rejects_malformed_hash():
    assert not PasswordHasher().verify("not-a-hash", "anything")


def test_fresh_hash_does_not_need_rehash():
    hasher = PasswordHasher()
    assert not hasher.needs_rehash(hasher.hash("password123"))
