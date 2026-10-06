"""Credential Vault: sealing, opening, and refusing.

The vault is imported inside each test rather than at module scope: conftest
purges the app modules before every test so config re-reads the environment,
and a module-level import would be bound to whichever version happened to be
loaded first. It is fetched through the fixture rather than with a local
`import relay.store.vault`, which would shadow the fixture name.
"""
import pytest

from conftest import FAKE_TOKEN


@pytest.fixture
def vault():
    import relay.store.vault as mod
    return mod


def test_round_trip_preserves_the_value(vault):
    blob = vault.seal(FAKE_TOKEN, "testpass123")
    assert vault.open_sealed(FAKE_TOKEN, blob) == "testpass123"


def test_ciphertext_does_not_contain_the_plaintext(vault):
    blob = vault.seal(FAKE_TOKEN, "correct horse battery staple")
    assert "correct horse" not in blob
    assert "battery" not in blob


def test_nonce_is_unique_per_seal(vault):
    # Two seals of the same value must differ, or identical passwords would be
    # visible as identical ciphertext in the database.
    assert vault.seal(FAKE_TOKEN, "same") != vault.seal(FAKE_TOKEN, "same")


@pytest.mark.parametrize("blob", [
    None, "", "not-a-blob", "v1.only-two-parts", "v9.a.b",
    "vx.bm90LWEuY29tcGF0aWJsZQ==.Y3Q=",          # wrong version
])
def test_malformed_entries_are_refused(vault, blob):
    with pytest.raises(vault.DecryptionFailed):
        vault.open_sealed(FAKE_TOKEN, blob)


def test_a_different_token_cannot_open_it(vault):
    blob = vault.seal(FAKE_TOKEN, "testpass123")
    with pytest.raises(vault.DecryptionFailed):
        vault.open_sealed("999:DIFFERENTTOKEN", blob)


def test_tampered_ciphertext_is_refused(vault):
    blob = vault.seal(FAKE_TOKEN, "testpass123")
    tampered = blob[:-8] + "AAAAAAAA"
    with pytest.raises(vault.DecryptionFailed):
        vault.open_sealed(FAKE_TOKEN, tampered)


def test_empty_token_cannot_derive_a_key(vault):
    with pytest.raises(vault.DecryptionFailed):
        vault.seal("", "testpass123")


def test_can_open_is_a_cheap_probe(vault):
    blob = vault.seal(FAKE_TOKEN, "testpass123")
    assert vault.can_open(FAKE_TOKEN, blob) is True
    assert vault.can_open("999:OTHER", blob) is False


def test_fingerprint_is_stable_and_not_the_username(vault):
    a = vault.fingerprint(FAKE_TOKEN, "testuser")
    assert a == vault.fingerprint(FAKE_TOKEN, "testuser")
    assert a != vault.fingerprint(FAKE_TOKEN, "someone-else")
    assert a != vault.fingerprint("999:OTHER", "testuser")
    assert "testuser" not in a


def test_fingerprint_is_short_enough_to_display(vault):
    assert len(vault.fingerprint(FAKE_TOKEN, "testuser")) == 16
