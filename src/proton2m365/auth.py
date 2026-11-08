"""Sign-in to Microsoft 365 or Outlook.com with MSAL: one account, tokens for both Graph and IMAP.

proton2m365 ships no sign-in app of its own: each user registers one (docs/setup-own-app.md), a public client
with an `http://localhost` redirect and the delegated permissions Mail.ReadWrite, MailboxSettings.ReadWrite and
IMAP.AccessAsUser.All (its token is requested for the Outlook resource), and gives its client ID with
--client-id, PROTON2M365_CLIENT_ID or client_id in config.toml. Tokens are cached in the OS secret store,
falling back to an owner-only file.
"""

from __future__ import annotations

import os
import stat
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GRAPH_SCOPES = [
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/MailboxSettings.ReadWrite",
]
IMAP_SCOPES = ["https://outlook.office.com/IMAP.AccessAsUser.All"]
CONFIG_DIR = Path.home() / ".config" / "proton2m365"
DEFAULT_CACHE = CONFIG_DIR / "token_cache.bin"
DEFAULT_CONFIG = CONFIG_DIR / "config.toml"
DEFAULT_TENANT = "common"
CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"  # personal Microsoft accounts
BROWSER_TIMEOUT_SECONDS = 300
ENV_CLIENT_ID = "PROTON2M365_CLIENT_ID"
ENV_TENANT = "PROTON2M365_TENANT"


class AuthError(Exception):
    pass


def _token_cache(path: Path):
    """MSAL cache persisted in the Keychain; falls back to an owner-only file if Keychain is unavailable."""
    from msal_extensions import FilePersistence, PersistedTokenCache, build_encrypted_persistence
    from msal_extensions.persistence import PersistenceNotFound

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        persistence = build_encrypted_persistence(str(path))
        # The Keychain can be built yet refuse writes (no default keychain, or a locked one over SSH:
        # OSError -25307/-25308). Write what is there now, so that failure shows up here and not mid sign-in.
        try:
            current = persistence.load()
        except PersistenceNotFound:
            current = None
        persistence.save(current if current else "{}")
    except Exception:  # noqa: BLE001 - any Keychain failure means: use the 0600 file instead
        persistence = FilePersistence(str(path))
        path.touch(exist_ok=True)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return PersistedTokenCache(persistence)


@dataclass(frozen=True)
class AppConfig:
    client_id: str
    tenant: str
    client_id_source: str  # flag, environment or config.toml (printed with sign-in errors)


def load_config(path: Path | None = None) -> dict:
    path = path or DEFAULT_CONFIG
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AuthError(f"cannot read {path}: {exc}") from exc


def resolve_app_config(client_id: str | None, tenant: str | None, *, config: dict | None = None) -> AppConfig:
    """Client ID and tenant, first found wins: flag, environment, config.toml. There is no built-in app."""
    config = load_config() if config is None else config
    sources = (("flag", client_id), ("environment", os.environ.get(ENV_CLIENT_ID)),
               ("config.toml", config.get("client_id")))
    source, cid = next(((s, v) for s, v in sources if v), (None, ""))
    if not cid:
        raise AuthError("P2M-AUTH-NO-APP: proton2m365 needs your own sign-in app. If you administer a Microsoft 365 "
                        "work or school tenant, run `proton2m365 setup` (one sign-in, about a minute). Otherwise "
                        "register it by hand (docs/setup-own-app.md) and pass --client-id and --tenant, or put "
                        f"client_id and tenant in {DEFAULT_CONFIG}")
    tenant = tenant or os.environ.get(ENV_TENANT) or config.get("tenant") or DEFAULT_TENANT
    return AppConfig(cid, tenant, source)


def headless() -> bool:
    """No browser can open here: an SSH session, or Linux without a display."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return False


def home_account_id(claims: dict) -> str | None:
    oid, tid = claims.get("oid"), claims.get("tid")
    return f"{oid}.{tid}" if oid and tid else None


class Auth:
    """Holds one signed-in account; hands out fresh Graph and IMAP access tokens."""

    def __init__(
        self,
        config: AppConfig,
        *,
        device_code: bool = False,
        account: str | None = None,
        cache_path: Path = DEFAULT_CACHE,
        app: Any = None,
        say: Callable[[str], None] = print,
        is_headless: Callable[[], bool] = headless,
    ):
        if app is None:
            import msal

            app = msal.PublicClientApplication(
                config.client_id,
                authority=f"https://login.microsoftonline.com/{config.tenant}",
                token_cache=_token_cache(Path(cache_path)),
            )
        self.app = app
        self.config = config
        self.device_code = device_code
        self.wanted = account  # --account <address>
        self.say = say
        self.is_headless = is_headless
        self.account: dict | None = None

    @property
    def username(self) -> str:
        if self.account is None:
            raise AuthError("not signed in")
        return self.account["username"]

    @property
    def home_account_id(self) -> str:
        if self.account is None:
            raise AuthError("not signed in")
        return self.account["home_account_id"]

    @property
    def is_personal(self) -> bool:
        """Outlook.com / Microsoft 365 Personal or Family: the consumer tenant."""
        return self.home_account_id.endswith("." + CONSUMER_TENANT)

    def sign_in(self, *, recorded: str | None = None) -> str:
        """Use the right cached account if its tokens still refresh; otherwise sign in. Returns the username.

        `recorded` is the home_account_id a state file is bound to: it picks the cached account when there
        are several, and a different account is never used for that state.
        """
        if self.sign_in_silent(recorded=recorded):
            return self.username
        return self._interactive()

    def sign_in_silent(self, *, recorded: str | None = None) -> str | None:
        """Use a cached account whose tokens still refresh; never open a browser or ask for a code."""
        cached = self.app.get_accounts()
        if self.wanted:
            cached = [a for a in cached if a.get("username", "").casefold() == self.wanted.casefold()]
        elif recorded:
            cached = [a for a in cached if a.get("home_account_id") == recorded] or cached
        if len(cached) > 1:
            names = ", ".join(sorted(a.get("username", "?") for a in cached))
            raise AuthError(f"P2M-AUTH-WHICH-ACCOUNT: several accounts are signed in ({names}); choose one with --account <address>")
        for account in cached:
            if self.app.acquire_token_silent(GRAPH_SCOPES, account=account):
                self.account = account
                return self.username
        return None

    def _interactive(self) -> str:
        self.say("Signing in to Microsoft. proton2m365 needs access to your mail to upload your export and set "
                 "categories, using the app you registered; nothing leaves this computer except calls to Microsoft.")
        device = self.device_code
        if not device and self.is_headless():
            self.say("No browser is available here (SSH session or no display), so signing in with a device code.")
            device = True
        if device:
            self.say("Device-code sign-in may ask twice: once for Microsoft Graph, then once for IMAP.")
            first = self._device_flow(GRAPH_SCOPES)
        else:
            self.say("Opening your browser to sign in. Choose the mailbox you are migrating into.")
            try:
                first = self.app.acquire_token_interactive(
                    GRAPH_SCOPES, extra_scopes_to_consent=IMAP_SCOPES, prompt="select_account",
                    login_hint=self.wanted, timeout=BROWSER_TIMEOUT_SECONDS,
                )
            except RuntimeError as exc:  # msal raises BrowserInteractionTimeoutError (a RuntimeError)
                if "Timeout" not in type(exc).__name__:
                    raise
                raise AuthError(f"P2M-AUTH-TIMEOUT: no browser completed sign-in within {BROWSER_TIMEOUT_SECONDS // 60} minutes; "
                                "run the command again with --device-code") from exc
        self._raise_for(first)
        self.account = self._account_for(first)
        if device and not self.app.acquire_token_silent(IMAP_SCOPES, account=self.account):
            second = self._device_flow(IMAP_SCOPES)
            self._raise_for(second)
            if home_account_id(second.get("id_token_claims") or {}) not in (None, self.home_account_id):
                self.account = None
                raise AuthError("P2M-AUTH-TWO-ACCOUNTS: the two device-code prompts were answered with different accounts; "
                                "run the command again and use the same mailbox both times")
        return self.username

    def _device_flow(self, scopes: list[str]) -> dict:
        flow = self.app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise AuthError(f"device code sign-in failed: {flow.get('error_description', flow)}")
        self.say(flow["message"])
        return self.app.acquire_token_by_device_flow(flow)

    def _account_for(self, result: dict) -> dict:
        """The account the user actually chose, by its object and tenant IDs, never just the first cached one."""
        wanted = home_account_id(result.get("id_token_claims") or {})
        accounts = self.app.get_accounts()
        match = [a for a in accounts if a.get("home_account_id") == wanted] if wanted else accounts[:1]
        if not match:
            raise AuthError("sign-in succeeded but the account is not in the token cache; run the command again")
        return match[0]

    def _raise_for(self, result: dict) -> None:
        if "access_token" in result:
            return
        error, detail = result.get("error", "?"), result.get("error_description", "")
        where = (f"(client ID from {self.config.client_id_source}, authority "
                 f"https://login.microsoftonline.com/{self.config.tenant})")
        if "AADSTS50194" in detail or "AADSTS700016" in detail:
            raise AuthError(
                "P2M-AUTH-TENANT: this app is registered for one organization, so sign-in must name it: pass --tenant "
                "with your tenant ID or domain (or set tenant in config.toml); for a personal-accounts app use "
                f"--tenant consumers {where}")
        if "AADSTS65001" in detail or (error in ("consent_required", "interaction_required") and "consent" in detail):
            raise AuthError(
                "P2M-AUTH-CONSENT: an administrator has not granted this app its permissions yet. In the Entra admin "
                "center, open the app, API permissions, and select \"Grant admin consent\" (docs/setup-own-app.md, "
                f"step 3), or have your admin open https://login.microsoftonline.com/{self.config.tenant}/adminconsent"
                f"?client_id={self.config.client_id} {where}")
        if "AADSTS90094" in detail or "AADSTS90095" in detail:
            raise AuthError(f"P2M-AUTH-CONSENT: an administrator must approve this app (user consent is blocked): {detail} {where}")
        raise AuthError(f"P2M-AUTH-FAILED: sign-in failed: {error}: {detail} {where}")

    def _token(self, scopes: list[str]) -> str:
        if self.account is None:
            raise AuthError("not signed in")
        result = self.app.acquire_token_silent(scopes, account=self.account)
        if not result or "access_token" not in result:
            detail = (result or {}).get("error_description", "no cached token")
            raise AuthError(f"P2M-AUTH-TOKEN: could not get a token for {scopes[0]}: {detail}; run the command again to sign in")
        return result["access_token"]

    def graph_token(self) -> str:
        return self._token(GRAPH_SCOPES)

    def imap_token(self) -> str:
        return self._token(IMAP_SCOPES)
