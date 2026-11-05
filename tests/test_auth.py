import base64
import json
import time

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError

from weekend_saver.auth import ARM_SCOPE, NotSignedInError, SignedInCredential


def jwt(**claims) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def token(account: str = "me@example.com", lifetime: int = 3600) -> AccessToken:
    return AccessToken(jwt(upn=account, tid="tenant-1"), int(time.time()) + lifetime)


class FakeCredential:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = 0

    def get_token(self, *scopes, **kwargs) -> AccessToken:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeRecord:
    def serialize(self) -> str:
        return '{"username": "me@example.com"}'


class BrowserFactory:
    """Records how browser credentials are built and what they return."""

    def __init__(self, result) -> None:
        self.result = result
        self.built_with: list[dict] = []
        self.authenticated_scopes: list[list[str]] = []

    def __call__(self, **kwargs):
        self.built_with.append(kwargs)
        factory = self

        class Browser(FakeCredential):
            def authenticate(self, *, scopes):
                factory.authenticated_scopes.append(scopes)
                return FakeRecord()

        return Browser(self.result)


def credential(tmp_path, cli_result, browser_result=None) -> tuple[SignedInCredential, dict]:
    cli = FakeCredential(cli_result)
    browser = BrowserFactory(browser_result or CredentialUnavailableError("no cached sign-in"))
    signed_in = SignedInCredential(
        tmp_path / "auth-record.json", cli_factory=lambda **_: cli, browser_factory=browser
    )
    return signed_in, {"cli": cli, "browser": browser}


def test_uses_the_azure_cli_session_when_there_is_one(tmp_path):
    signed_in, fakes = credential(tmp_path, token())

    assert signed_in.get_token(ARM_SCOPE).token == token().token
    assert fakes["browser"].built_with == []


def test_falls_back_to_the_cached_browser_sign_in(tmp_path):
    cli_down = CredentialUnavailableError("Please run 'az login'")
    signed_in, fakes = credential(tmp_path, cli_down, token("browser@example.com"))

    status = signed_in.status()

    assert status.signed_in
    assert status.account == "browser@example.com"
    assert status.source == "browser"
    # Unattended runs must never open a browser window by themselves
    assert fakes["browser"].built_with[0]["disable_automatic_authentication"] is True


def test_raises_not_signed_in_when_neither_source_works(tmp_path):
    signed_in, _ = credential(tmp_path, ClientAuthenticationError("az failed"))

    with pytest.raises(NotSignedInError, match="az login"):
        signed_in.get_token(ARM_SCOPE)


def test_status_reports_account_and_tenant_from_the_token(tmp_path):
    signed_in, _ = credential(tmp_path, token("ops@example.com"))

    status = signed_in.status()

    assert (status.signed_in, status.account, status.tenant_id, status.source) == (
        True,
        "ops@example.com",
        "tenant-1",
        "azure-cli",
    )


def test_status_drops_the_identity_provider_prefix_of_personal_accounts(tmp_path):
    signed_in, _ = credential(tmp_path, token("live.com#me@hotmail.com"))

    assert signed_in.status().account == "me@hotmail.com"


def test_status_says_not_signed_in_instead_of_raising(tmp_path):
    signed_in, _ = credential(tmp_path, CredentialUnavailableError("no az"))

    status = signed_in.status()

    assert not status.signed_in
    assert status.account == ""


def test_tokens_are_reused_until_they_are_close_to_expiry(tmp_path):
    signed_in, fakes = credential(tmp_path, token())

    signed_in.get_token(ARM_SCOPE)
    signed_in.get_token(ARM_SCOPE)

    assert fakes["cli"].calls == 1


def test_a_nearly_expired_token_is_fetched_again(tmp_path):
    signed_in, fakes = credential(tmp_path, token(lifetime=30))

    signed_in.get_token(ARM_SCOPE)
    signed_in.get_token(ARM_SCOPE)

    assert fakes["cli"].calls == 2


def test_tokens_for_another_tenant_are_not_served_from_the_cache(tmp_path):
    signed_in, fakes = credential(tmp_path, token())

    signed_in.get_token(ARM_SCOPE)
    signed_in.get_token(ARM_SCOPE, tenant_id="other-tenant")
    signed_in.get_token(ARM_SCOPE, tenant_id="other-tenant")

    assert fakes["cli"].calls == 2


def test_a_claims_challenge_always_fetches_a_new_token(tmp_path):
    signed_in, fakes = credential(tmp_path, token())

    signed_in.get_token(ARM_SCOPE)
    signed_in.get_token(ARM_SCOPE, claims="challenge")
    signed_in.get_token(ARM_SCOPE, claims="challenge")

    assert fakes["cli"].calls == 3


def test_the_first_source_can_be_swapped_and_named(tmp_path):
    primary = FakeCredential(token("pipeline@example.com"))
    signed_in = SignedInCredential(
        tmp_path / "auth-record.json",
        cli_factory=lambda **_: primary,
        browser_factory=BrowserFactory(CredentialUnavailableError("none")),
        primary_name="default credential chain",
    )

    assert signed_in.status().source == "default credential chain"


def test_login_saves_the_record_and_later_credentials_use_it(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "weekend_saver.auth.AuthenticationRecord.deserialize", staticmethod(lambda text: text)
    )
    cli_down = CredentialUnavailableError("no az")
    signed_in, fakes = credential(tmp_path, cli_down, token("browser@example.com"))

    status = signed_in.login()

    record_file = tmp_path / "auth-record.json"
    assert fakes["browser"].authenticated_scopes == [[ARM_SCOPE]]
    assert record_file.read_text(encoding="utf-8") == FakeRecord().serialize()
    assert status.signed_in
    assert fakes["browser"].built_with[-1]["authentication_record"] == FakeRecord().serialize()


def test_an_unreadable_record_file_is_ignored(tmp_path):
    (tmp_path / "auth-record.json").write_text("{not a record", encoding="utf-8")
    cli_down = CredentialUnavailableError("no az")
    signed_in, fakes = credential(tmp_path, cli_down, token())

    assert signed_in.status().signed_in
    assert fakes["browser"].built_with[0]["authentication_record"] is None
