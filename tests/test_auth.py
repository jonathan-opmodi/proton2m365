from __future__ import annotations

import pytest

from proton2m365 import auth as auth_mod
from proton2m365.auth import (
    CONSUMER_TENANT, GRAPH_SCOPES, IMAP_SCOPES, AppConfig, Auth, AuthError, resolve_app_config,
)

from .conftest import owner_only

WORK = {"username": "alice@example.com", "home_account_id": "oid-a.tid-work"}
OTHER = {"username": "bob@example.com", "home_account_id": "oid-b.tid-work"}
PERSONAL = {"username": "carol@example.org", "home_account_id": f"oid-c.{CONSUMER_TENANT}"}
CONFIG = AppConfig("cid", "common", "flag")


def claims(account):
    oid, tid = account["home_account_id"].split(".", 1)
    return {"oid": oid, "tid": tid}


class FakeApp:
    """MSAL's PublicClientApplication, as far as Auth uses it."""

    def __init__(self, *, cached=(), choose=WORK, imap_choose=None, sign_in_ok=True, imap_silent=True,
                 timeout=False):
        self.accounts = list(cached)
        self.choose = choose                    # the account the user picks in the browser / device prompt
        self.imap_choose = imap_choose or choose
        self.sign_in_ok = sign_in_ok
        self.imap_silent = imap_silent          # whether IMAP consent came with the Graph sign-in
        self.timeout = timeout
        self.calls = []

    def get_accounts(self):
        return list(self.accounts)

    def acquire_token_silent(self, scopes, account):
        self.calls.append(("silent", tuple(scopes), account["username"]))
        if account not in self.accounts or (scopes == IMAP_SCOPES and not self.imap_silent):
            return None
        return {"access_token": "graph-tok" if scopes == GRAPH_SCOPES else "imap-tok"}

    def acquire_token_interactive(self, scopes, **kwargs):
        self.calls.append(("interactive", tuple(scopes), tuple(kwargs.get("extra_scopes_to_consent") or ()),
                           kwargs.get("timeout")))
        if self.timeout:
            raise type("BrowserInteractionTimeoutError", (RuntimeError,), {})("no response")
        return self._finish(self.choose)

    def initiate_device_flow(self, scopes):
        self.calls.append(("device", tuple(scopes)))
        return {"user_code": "ABC", "message": f"enter ABC for {scopes[0]}", "scopes": scopes}

    def acquire_token_by_device_flow(self, flow):
        if flow["scopes"] == IMAP_SCOPES:
            self.imap_silent = True
            return self._finish(self.imap_choose)
        return self._finish(self.choose)

    def _finish(self, account):
        if not self.sign_in_ok:
            return {"error": "access_denied", "error_description": "AADSTS53003 blocked by Conditional Access"}
        if account not in self.accounts:
            self.accounts.append(account)
        return {"access_token": "t", "id_token_claims": claims(account)}


def make(app, **kwargs):
    said = []
    kwargs.setdefault("is_headless", lambda: False)
    return Auth(CONFIG, app=app, say=said.append, **kwargs), said


def test_cached_account_skips_sign_in():
    app = FakeApp(cached=[WORK])
    auth, _ = make(app)
    assert auth.sign_in() == "alice@example.com"
    assert not [c for c in app.calls if c[0] in ("interactive", "device")]


def test_browser_sign_in_consents_both_resources_with_a_timeout():
    app = FakeApp()
    auth, said = make(app)
    auth.sign_in()
    assert ("interactive", tuple(GRAPH_SCOPES), tuple(IMAP_SCOPES), auth_mod.BROWSER_TIMEOUT_SECONDS) in app.calls
    assert "Choose the mailbox you are migrating into" in " ".join(said)
    assert (auth.graph_token(), auth.imap_token()) == ("graph-tok", "imap-tok")


def test_browser_timeout_suggests_device_code():
    auth, _ = make(FakeApp(timeout=True))
    with pytest.raises(AuthError, match="--device-code"):
        auth.sign_in()


def test_the_chosen_account_is_used_not_the_first_cached_one():
    # Another account is cached but its refresh token is dead; the user picks bob in the browser.
    app = FakeApp(cached=[WORK], choose=OTHER)
    app.acquire_token_silent = lambda scopes, account: None if account is WORK else {"access_token": "t"}
    auth, _ = make(app)
    assert auth.sign_in() == "bob@example.com"


def test_headless_uses_device_code_automatically():
    app = FakeApp()
    auth, said = make(app, is_headless=lambda: True)
    auth.sign_in()
    assert ("device", tuple(GRAPH_SCOPES)) in app.calls and not [c for c in app.calls if c[0] == "interactive"]
    assert "device code" in " ".join(said)


def test_device_code_asks_again_for_imap_when_needed():
    app = FakeApp(imap_silent=False)
    auth, said = make(app, device_code=True)
    auth.sign_in()
    assert [c for c in app.calls if c[0] == "device"] == [("device", tuple(GRAPH_SCOPES)),
                                                         ("device", tuple(IMAP_SCOPES))]
    assert "may ask twice" in " ".join(said)


def test_device_code_refuses_two_different_accounts():
    app = FakeApp(imap_silent=False, choose=WORK, imap_choose=OTHER)
    auth, _ = make(app, device_code=True)
    with pytest.raises(AuthError, match="different accounts"):
        auth.sign_in()
    with pytest.raises(AuthError, match="not signed in"):
        auth.graph_token()


def test_several_cached_accounts_need_a_choice():
    auth, _ = make(FakeApp(cached=[WORK, OTHER]))
    with pytest.raises(AuthError, match="--account"):
        auth.sign_in()


def test_account_flag_and_recorded_account_pick_from_the_cache():
    auth, _ = make(FakeApp(cached=[WORK, OTHER]), account="BOB@example.com")
    assert auth.sign_in() == "bob@example.com"
    auth, _ = make(FakeApp(cached=[WORK, OTHER]))
    assert auth.sign_in(recorded=OTHER["home_account_id"]) == "bob@example.com"


def test_personal_account_is_detected_by_tenant():
    auth, _ = make(FakeApp(cached=[PERSONAL]))
    auth.sign_in()
    assert auth.is_personal
    auth, _ = make(FakeApp(cached=[WORK]))
    auth.sign_in()
    assert not auth.is_personal


def test_sign_in_failure_is_explained_with_its_context():
    auth, _ = make(FakeApp(sign_in_ok=False))
    with pytest.raises(AuthError, match=r"Conditional Access .*client ID from flag.*common"):
        auth.sign_in()


def test_admin_consent_error_gives_the_link():
    app = FakeApp()
    app._finish = lambda account: {"error": "invalid_grant",
                                   "error_description": "AADSTS65001: The user or administrator has not consented"}
    auth, _ = make(app)
    with pytest.raises(AuthError, match=r"P2M-AUTH-CONSENT.*Grant admin consent.*adminconsent\?client_id=cid"):
        auth.sign_in()


def test_tokens_require_sign_in():
    auth, _ = make(FakeApp())
    with pytest.raises(AuthError, match="not signed in"):
        auth.graph_token()


def test_resolve_app_config_order(monkeypatch):
    monkeypatch.delenv("PROTON2M365_CLIENT_ID", raising=False)
    monkeypatch.delenv("PROTON2M365_TENANT", raising=False)
    cfg = {"client_id": "toml-cid", "tenant": "organizations"}
    assert resolve_app_config(None, None, config=cfg) == AppConfig("toml-cid", "organizations", "config.toml")
    assert resolve_app_config(None, None, config={"client_id": "c"}) == AppConfig("c", "common", "config.toml")
    monkeypatch.setenv("PROTON2M365_CLIENT_ID", "env-cid")
    monkeypatch.setenv("PROTON2M365_TENANT", "example.com")
    assert resolve_app_config(None, None, config=cfg) == AppConfig("env-cid", "example.com", "environment")
    assert resolve_app_config("flag-cid", "consumers", config=cfg) == AppConfig("flag-cid", "consumers", "flag")


def test_no_app_says_how_to_register_one(monkeypatch):
    monkeypatch.delenv("PROTON2M365_CLIENT_ID", raising=False)
    with pytest.raises(AuthError, match=r"P2M-AUTH-NO-APP: proton2m365 needs your own sign-in app.*setup-own-app"):
        resolve_app_config(None, None, config={})


def test_single_tenant_app_without_tenant_says_to_pass_it():
    app = FakeApp()
    app._finish = lambda account: {"error": "invalid_request", "error_description":
                                   "AADSTS50194: Application is not configured as a multi-tenant application."}
    auth, _ = make(app)
    with pytest.raises(AuthError, match=r"P2M-AUTH-TENANT.*--tenant"):
        auth.sign_in()


def test_config_toml_is_read(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('client_id = "toml-cid"\ntenant = "consumers"\n')
    assert auth_mod.load_config(path) == {"client_id": "toml-cid", "tenant": "consumers"}
    path.write_text("client_id = \n")
    with pytest.raises(AuthError, match="cannot read"):
        auth_mod.load_config(path)


@pytest.mark.parametrize(("env", "platform", "expected"), [
    ({"SSH_CONNECTION": "1 2 3 4"}, "darwin", True),
    ({}, "darwin", False),
    ({}, "linux", True),
    ({"DISPLAY": ":0"}, "linux", False),
    ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", False),
])
def test_headless_detection(monkeypatch, env, platform, expected):
    for key in ("SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(auth_mod.sys, "platform", platform)
    assert auth_mod.headless() is expected


def test_token_cache_falls_back_to_an_owner_only_file(tmp_path, monkeypatch):
    """Headless Linux has no secret service: the cache must still work, readable only by the user."""
    import msal_extensions

    def no_keyring(path):
        raise RuntimeError("no D-Bus session")

    monkeypatch.setattr(msal_extensions, "build_encrypted_persistence", no_keyring)
    cache = auth_mod._token_cache(tmp_path / "cfg" / "token_cache.bin")
    assert cache is not None
    assert owner_only(tmp_path / "cfg" / "token_cache.bin")


def test_token_cache_falls_back_when_the_keychain_refuses_writes(tmp_path, monkeypatch):
    """macOS can build a Keychain persistence that then refuses to save (no default keychain, or a locked one
    over SSH: OSError -25307/-25308). That must fall back to the owner-only file, not crash mid sign-in."""
    import msal_extensions
    from msal_extensions.persistence import FilePersistence, PersistenceNotFound

    class LockedKeychain:
        def load(self):
            raise PersistenceNotFound(message="empty")

        def save(self, content):
            raise OSError(-25307)

        def get_location(self):
            return str(tmp_path / "keychain")

    monkeypatch.setattr(msal_extensions, "build_encrypted_persistence", lambda path: LockedKeychain())
    cache = auth_mod._token_cache(tmp_path / "cfg" / "token_cache.bin")
    assert isinstance(cache._persistence, FilePersistence)
    assert owner_only(tmp_path / "cfg" / "token_cache.bin")
