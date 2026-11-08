"""`proton2m365 setup`: register the sign-in app proton2m365 needs, in the admin's own tenant, in one sign-in.

The admin signs in through Microsoft Graph Command Line Tools, a Microsoft first-party public client, asking only
for Application.ReadWrite.All and DelegatedPermissionGrant.ReadWrite.All. That token lives in an in-memory cache:
nothing able to create apps is ever written to disk (token_cache.bin is never touched). The Graph Command Line
Tools client itself never gets mail permissions; the app setup creates does, and it is the user's own.

    lock config.toml ─ sign in (Graph CLI, memory only) ─ read the CLI's grants
      ─ adopt the recorded app, or write a pending record (nonce) and create one tagged with it
      ─ service principal ─ Exchange Online service principal (for IMAP) ─ admin consent (two grants)
      ─ wait for the service principal ─ write client_id, tenant and [setup] to config.toml
      ─ revoke the CLI's grant if it holds nothing but setup's own permissions (setup made it)

A pending record is written before anything is created, and the app carries its nonce in a tag, so an
interrupted run is picked up by the next one instead of creating a second app. Creates are never repeated
blindly after an uncertain error (GraphStore.create_once looks first). Only an app recorded in config.toml and
tagged with that record's nonce is ever updated or removed; a same-named app setup did not create is left alone.

When the tenant blocks the Graph CLI client or the admin lacks a role, setup writes an Azure CLI script that
creates the same app and exits 2 with P2M-SETUP-BLOCKED; `setup --use-app CLIENT_ID` then records it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import auth as auth_mod
from .graph_store import GraphError, GraphStore
from .locks import FileLock, LockHeld

GRAPH_CLI = "14d82eec-204b-4c2f-b7e8-296a70dab67e"     # Microsoft Graph Command Line Tools (first party)
GRAPH_APP = "00000003-0000-0000-c000-000000000000"     # Microsoft Graph
EXCHANGE_APP = "00000002-0000-0ff1-ce00-000000000000"  # Office 365 Exchange Online (IMAP)
SETUP_SCOPE_NAMES = ("Application.ReadWrite.All", "DelegatedPermissionGrant.ReadWrite.All")
SETUP_SCOPES = [f"https://graph.microsoft.com/{s}" for s in SETUP_SCOPE_NAMES]
SIGN_IN_SCOPES = {"openid", "profile", "offline_access", "email"}
GRAPH_PERMISSIONS = ("Mail.ReadWrite", "MailboxSettings.ReadWrite", "Contacts.ReadWrite", "Calendars.ReadWrite")
EXCHANGE_PERMISSIONS = ("IMAP.AccessAsUser.All",)
# The same permissions by id, for the Azure CLI fallback script (the Graph path looks them up in the tenant).
PERMISSION_IDS = {
    "Mail.ReadWrite": "e383f46e-2787-4529-855e-0e479a3ffac0",
    "MailboxSettings.ReadWrite": "818c620a-27a9-40bd-a6a5-d96f7d610b4b",
    "Contacts.ReadWrite": "d56682ec-c09e-4743-aaf4-1a3aac4caa21",
    "Calendars.ReadWrite": "1ec239c2-d7c9-4623-a91a-a9775856bb36",
    "IMAP.AccessAsUser.All": "652390e4-393a-48de-9484-05f9b1212954",
}
APP_NAME = "proton2m365"
TAG = "proton2m365"
NONCE_TAG = "proton2m365-nonce:"
SP_WAIT_SECONDS = 120
DEFAULT_TENANT = "organizations"
BLOCKED_SIGN_IN = ("AADSTS65001", "AADSTS90094", "AADSTS90095", "AADSTS50105", "AADSTS53003", "AADSTS7000112",
                   "AADSTS65004")
PERSONAL_SIGN_IN = ("AADSTS50020", "AADSTS500200", "AADSTS9002331")


class SetupError(Exception):
    """A failure with a P2M- code; the CLI exits 2."""


class SetupBlocked(SetupError):
    def __init__(self, message: str, script: Path | None):
        super().__init__(message)
        self.script = script


@dataclass
class SetupResult:
    client_id: str = ""
    tenant: str = ""
    app_object_id: str = ""
    adopted: bool = False             # an app from an earlier run was found and updated
    created: list[str] = field(default_factory=list)   # what this run created
    cli_grant: str = "none"           # none | revoked | kept (existed with other permissions)
    foreign_apps: int = 0             # apps named proton2m365 that setup did not create (left alone)
    dry_run: bool = False
    plan: list[str] = field(default_factory=list)      # --dry-run: what would happen


# --- config.toml ----------------------------------------------------------------------------------

def read_record(config: dict) -> dict:
    record = config.get("setup")
    return dict(record) if isinstance(record, dict) else {}


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid TOML basic string


def config_text(existing: str, *, top: dict[str, str | None], setup: dict | None) -> str:
    """config.toml with these top-level keys set (None removes one) and the [setup] table replaced
    (None removes it). Every other line, table and comment is kept."""
    lines = existing.splitlines()
    first_table = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    head, tail = lines[:first_table], lines[first_table:]
    key = re.compile(r"\s*([A-Za-z0-9_-]+)\s*=")
    head = [line for line in head if not ((m := key.match(line)) and m.group(1) in top)]
    head = [f"{k} = {_toml_value(v)}" for k, v in top.items() if v is not None] + head
    out, skipping = [], False
    for line in tail:
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped == "[setup]"
        if not skipping:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    if setup:
        out += ([""] if (head or out) else []) + ["[setup]"] + [f"{k} = {_toml_value(v)}" for k, v in setup.items()]
    while head and not head[-1].strip():
        head.pop()
    body = head + ([""] if head and out else []) + out
    return "\n".join(body) + "\n"


def write_config(path: Path, *, top: dict[str, str | None], setup: dict | None) -> None:
    """Rewrite config.toml atomically, owner-only. Raises OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.is_file() else ""
    text = config_text(existing, top=top, setup=setup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def config_lines(client_id: str, tenant: str) -> str:
    return f'client_id = "{client_id}"\ntenant = "{tenant}"'


# --- sign-in --------------------------------------------------------------------------------------

class AdminSignIn:
    """The admin's Graph token through Microsoft Graph Command Line Tools, cached in memory only."""

    def __init__(self, tenant: str = DEFAULT_TENANT, *, device_code: bool = False, app: Any = None,
                 say: Callable[[str], None] = print, is_headless: Callable[[], bool] = auth_mod.headless,
                 script_dir: Path | None = None):
        if app is None:
            import msal

            app = msal.PublicClientApplication(GRAPH_CLI, authority=f"https://login.microsoftonline.com/{tenant}",
                                               token_cache=msal.TokenCache())  # never persisted (D13)
        self.app, self.tenant, self.device_code, self.say, self.is_headless = app, tenant, device_code, say, is_headless
        self.script_dir = script_dir
        self.account: dict | None = None
        self.claims: dict = {}

    def sign_in(self) -> dict:
        self.say("Signing in as a Microsoft 365 administrator through Microsoft Graph Command Line Tools (a "
                 "Microsoft app) to register proton2m365's sign-in app in your tenant. It asks to manage apps and "
                 "permission grants; it never gets access to mail.")
        if self.device_code or self.is_headless():
            flow = self.app.initiate_device_flow(scopes=SETUP_SCOPES)
            if "user_code" not in flow:
                raise SetupError(f"P2M-AUTH-FAILED: device code sign-in failed: {flow.get('error_description', flow)}")
            self.say(flow["message"])
            result = self.app.acquire_token_by_device_flow(flow)
        else:
            self.say("Opening your browser to sign in. Choose an administrator account of the tenant you are "
                     "migrating into.")
            try:
                result = self.app.acquire_token_interactive(SETUP_SCOPES, prompt="select_account",
                                                            timeout=auth_mod.BROWSER_TIMEOUT_SECONDS)
            except RuntimeError as exc:
                if "Timeout" not in type(exc).__name__:
                    raise
                raise SetupError("P2M-AUTH-TIMEOUT: no browser completed sign-in within "
                                 f"{auth_mod.BROWSER_TIMEOUT_SECONDS // 60} minutes; run setup again with "
                                 "--device-code") from exc
        if "access_token" not in result:
            detail = f"{result.get('error', '?')}: {result.get('error_description', '')}"
            if any(code in detail for code in PERSONAL_SIGN_IN):
                raise SetupError(personal_message())
            if any(code in detail for code in BLOCKED_SIGN_IN):
                raise SetupBlocked(blocked_message("your organization does not allow Microsoft Graph Command Line "
                                                   f"Tools to sign in here ({detail[:200]})",
                                                   self._script(self.tenant)), self._script(self.tenant))
            raise SetupError(f"P2M-AUTH-FAILED: sign-in failed: {detail[:400]}")
        self.claims = result.get("id_token_claims") or {}
        if self.claims.get("tid") == auth_mod.CONSUMER_TENANT:
            raise SetupError(personal_message())
        accounts = self.app.get_accounts()
        self.account = accounts[0] if accounts else None
        self._token = result["access_token"]
        return self.claims

    def _script(self, tenant: str) -> Path | None:
        if self.script_dir is None:
            return None
        if not hasattr(self, "_written"):
            self._written = write_fallback_script(self.script_dir, tenant)
        return self._written

    def token(self) -> str:
        if self.account is not None:
            fresh = self.app.acquire_token_silent(SETUP_SCOPES, account=self.account)
            if fresh and "access_token" in fresh:
                return fresh["access_token"]
        return self._token


def personal_message() -> str:
    return ("P2M-SETUP-PERSONAL: this is a personal Microsoft account (Outlook.com, Microsoft 365 Personal or "
            "Family). Only an organization's administrator can register an app with setup; for a personal account, "
            "register the app by hand (docs/setup-own-app.md, route B), then put client_id and tenant = \"consumers\" "
            "in config.toml")


def blocked_message(why: str, script: Path | None) -> str:
    where = (f"run the Azure CLI script written to {script} (bash {script}, or in PowerShell: pwsh {script}); it "
             "creates the same app and prints the command that finishes setup" if script else
             "register the app by hand: docs/setup-own-app.md")
    return f"P2M-SETUP-BLOCKED: setup cannot register the app: {why}. Instead, {where}"


# --- the fallback script ----------------------------------------------------------------------------

def _resource_access() -> list[dict]:
    return [
        {"resourceAppId": GRAPH_APP, "resourceAccess": [
            {"id": PERMISSION_IDS[p], "type": "Scope"} for p in GRAPH_PERMISSIONS]},
        {"resourceAppId": EXCHANGE_APP, "resourceAccess": [
            {"id": PERMISSION_IDS[p], "type": "Scope"} for p in EXCHANGE_PERMISSIONS]},
    ]


def fallback_script(tenant: str, *, windows: bool = False) -> str:
    """An Azure CLI script that registers the same app, for tenants where setup cannot sign in."""
    rra = json.dumps(_resource_access())
    if windows:
        return f"""# proton2m365: register the sign-in app with the Azure CLI (setup could not do it directly).
# Needs the Azure CLI (az) and an administrator account. Creates no secrets.
$ErrorActionPreference = "Stop"
$Tenant = "{tenant}"
az login --tenant $Tenant --allow-no-subscriptions | Out-Null
$Rra = New-TemporaryFile
Set-Content -Path $Rra -Value '{rra}'
$AppId = az ad app create --display-name "{APP_NAME}" --sign-in-audience AzureADMyOrg --is-fallback-public-client true `
  --public-client-redirect-uris http://localhost --required-resource-accesses "@$Rra" --query appId -o tsv
Remove-Item $Rra
az ad sp create --id $AppId | Out-Null
az ad app permission admin-consent --id $AppId
$TenantId = az account show --query tenantId -o tsv
Write-Output "Done. Finish with: proton2m365 setup --use-app $AppId --tenant $TenantId"
"""
    return f"""#!/usr/bin/env bash
# proton2m365: register the sign-in app with the Azure CLI (setup could not do it directly).
# Needs the Azure CLI (az) and an administrator account. Creates no secrets.
set -euo pipefail
TENANT="{tenant}"
az login --tenant "$TENANT" --allow-no-subscriptions >/dev/null
RRA=$(mktemp)
cat > "$RRA" <<'JSON'
{rra}
JSON
APP_ID=$(az ad app create --display-name "{APP_NAME}" --sign-in-audience AzureADMyOrg --is-fallback-public-client true \\
  --public-client-redirect-uris http://localhost --required-resource-accesses "@$RRA" --query appId -o tsv)
rm -f "$RRA"
az ad sp create --id "$APP_ID" >/dev/null
az ad app permission admin-consent --id "$APP_ID"
TENANT_ID=$(az account show --query tenantId -o tsv)
echo "Done. Finish with: proton2m365 setup --use-app $APP_ID --tenant $TENANT_ID"
"""


def write_fallback_script(directory: Path, tenant: str, *, windows: bool | None = None) -> Path:
    windows = (os.name == "nt") if windows is None else windows
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("proton2m365-setup.ps1" if windows else "proton2m365-setup.sh")
    path.write_text(fallback_script(tenant, windows=windows), newline="\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


# --- the setup itself ---------------------------------------------------------------------------------

class Setup:
    def __init__(self, graph: GraphStore, config_path: Path, *, say: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 script_dir: Path | None = None, tenant_hint: str = DEFAULT_TENANT):
        self.graph, self.config_path, self.say, self.sleep, self.clock = graph, config_path, say, sleep, clock
        self.script_dir, self.tenant_hint = script_dir, tenant_hint

    # -- Graph helpers --

    def _get(self, url: str, **params) -> dict:
        return self.graph.request("GET", url, params=params or None)

    def _one(self, url: str, flt: str, select: str) -> dict | None:
        found = self._get(url, **{"$filter": flt, "$select": select}).get("value", [])
        return found[0] if found else None

    def _service_principal(self, app_id: str) -> dict | None:
        return self._one("/servicePrincipals", f"appId eq '{app_id}'", "id,appId,oauth2PermissionScopes")

    def _blocked(self, why: str) -> SetupBlocked:
        script = write_fallback_script(self.script_dir, self.tenant_hint) if self.script_dir else None
        return SetupBlocked(blocked_message(why, script), script)

    def _write(self, method: str, url: str, **kwargs) -> dict:
        """A change; a 403 means this admin cannot make it (a role is missing)."""
        try:
            return self.graph.request(method, url, **kwargs)
        except GraphError as exc:
            if exc.status == 403:
                script = write_fallback_script(self.script_dir, self.tenant_hint) if self.script_dir else None
                then = (f"; or have such an administrator run the Azure CLI script written to {script}" if script
                        else "")
                raise SetupBlocked(
                    "P2M-SETUP-ROLE: your account may not register apps or grant consent in this tenant. Sign in as "
                    "a Global Administrator, Privileged Role Administrator, Application Administrator or Cloud "
                    f"Application Administrator and run setup again{then} [{exc}]", script) from exc
            raise

    def _create(self, url: str, body: dict, exists: Callable[[], dict | None]) -> dict:
        def create():
            return self._write("POST", url, json=body)
        made = self.graph.create_once(create, lambda: exists() is not None)
        return made if made is not None else exists()

    # -- the Graph CLI's own grant (D22) --

    def cli_grants(self, me: str | None) -> list[dict]:
        cli = self._service_principal(GRAPH_CLI)
        if cli is None:
            return []
        grants = self._get("/oauth2PermissionGrants", **{"$filter": f"clientId eq '{cli['id']}'"}).get("value", [])
        return [g for g in grants if g.get("consentType") == "AllPrincipals" or g.get("principalId") == me]

    def settle_cli_grant(self, me: str | None) -> str:
        """Revoke the Graph CLI grant when it holds nothing but setup's own permissions: setup made it, and the
        tenant is left as it was. A grant with other permissions existed before (Graph PowerShell use): kept."""
        outcome = "none"
        for grant in self.cli_grants(me):
            scopes = set((grant.get("scope") or "").split())
            if scopes <= set(SETUP_SCOPE_NAMES) | SIGN_IN_SCOPES:
                self.graph.request("DELETE", f"/oauth2PermissionGrants/{grant['id']}")
                outcome = "revoked"
            elif outcome == "none":
                outcome = "kept"
        return outcome

    # -- run --

    def run(self, tenant: str, me: str | None, *, dry_run: bool = False) -> SetupResult:
        result = SetupResult(tenant=tenant, dry_run=dry_run)
        config = auth_mod.load_config(self.config_path)
        record = read_record(config)
        if record.get("tenant") and record["tenant"] != tenant:
            record = {}  # recorded for another tenant: not ours to adopt here
        try:
            self._run(result, tenant, record, config, dry_run)
        finally:
            try:
                result.cli_grant = self.settle_cli_grant(me)
            except GraphError as exc:
                self.say(f"Could not tidy up Microsoft Graph Command Line Tools' consent ({exc}); remove it in the "
                         "Entra admin center under Enterprise applications if you do not use it.")
        return result

    def _find_by_nonce(self, nonce: str) -> dict | None:
        return self._one("/applications", f"tags/any(t:t eq '{NONCE_TAG}{nonce}')", "id,appId,tags,displayName")

    def _app_body(self, nonce: str, graph_sp: dict, exchange_sp: dict) -> dict:
        def scopes(sp: dict, names) -> list[dict]:
            by_name = {s["value"]: s["id"] for s in sp.get("oauth2PermissionScopes", [])}
            missing = [n for n in names if n not in by_name]
            if missing:
                raise SetupError(f"P2M-SETUP-IMAP: the tenant's {sp.get('appId')} service does not offer {missing}")
            return [{"id": by_name[n], "type": "Scope"} for n in names]
        return {
            "displayName": APP_NAME, "signInAudience": "AzureADMyOrg", "isFallbackPublicClient": True,
            "publicClient": {"redirectUris": ["http://localhost"]},
            "tags": [TAG, f"{NONCE_TAG}{nonce}"],
            "requiredResourceAccess": [
                {"resourceAppId": GRAPH_APP, "resourceAccess": scopes(graph_sp, GRAPH_PERMISSIONS)},
                {"resourceAppId": EXCHANGE_APP, "resourceAccess": scopes(exchange_sp, EXCHANGE_PERMISSIONS)},
            ],
        }

    def _run(self, result: SetupResult, tenant: str, record: dict, config: dict, dry_run: bool) -> None:
        same_name = self._get("/applications", **{"$filter": f"displayName eq '{APP_NAME}'",
                                                   "$select": "id,appId,tags"}).get("value", [])
        nonce = record.get("nonce")
        app = None
        if record.get("app_object_id"):
            try:
                found = self._get(f"/applications/{record['app_object_id']}", **{"$select": "id,appId,tags"})
                if f"{NONCE_TAG}{nonce}" in (found.get("tags") or []):
                    app = found
            except GraphError as exc:
                if exc.status != 404:
                    raise
        if app is None and nonce:
            app = self._find_by_nonce(nonce)  # an earlier run stopped after creating it
        ours = {a["id"] for a in [app] if a}
        result.foreign_apps = len([a for a in same_name if a["id"] not in ours
                                   and not any(t.startswith(NONCE_TAG) for t in a.get("tags") or [])])

        graph_sp = self._service_principal(GRAPH_APP)
        if graph_sp is None:
            raise SetupError("P2M-SETUP-IMAP: Microsoft Graph is not available in this tenant")
        exchange_sp = self._service_principal(EXCHANGE_APP)
        if dry_run:
            result.plan = [
                ("update" if app else "create") + f" the app {APP_NAME!r} (public client, http://localhost) with "
                + ", ".join(GRAPH_PERMISSIONS + EXCHANGE_PERMISSIONS),
                "create its service principal" if not app else "check its service principal",
                "create the Office 365 Exchange Online service principal (for IMAP)" if exchange_sp is None else
                "use the existing Office 365 Exchange Online service principal (for IMAP)",
                "grant admin consent for those permissions (for every user of this tenant)",
                f"write client_id and tenant to {self.config_path}",
            ]
            result.adopted = app is not None
            result.app_object_id = (app or {}).get("id", "")
            result.client_id = (app or {}).get("appId", "")
            return
        if exchange_sp is None:
            try:
                exchange_sp = self._create("/servicePrincipals", {"appId": EXCHANGE_APP},
                                           lambda: self._service_principal(EXCHANGE_APP))
                result.created.append("Exchange Online service principal")
            except SetupError as exc:
                raise SetupError(f"P2M-SETUP-IMAP: Office 365 Exchange Online is not set up in this tenant and "
                                 f"setup could not add it ({exc}); IMAP needs it") from exc
            exchange_sp = self._service_principal(EXCHANGE_APP) or exchange_sp

        if app is None:
            nonce = secrets.token_hex(8)
            self._save(config, {"status": "pending", "tenant": tenant, "nonce": nonce}, keep_top=True)
            body = self._app_body(nonce, graph_sp, exchange_sp)
            app = self._create("/applications", body, lambda: self._find_by_nonce(nonce))
            result.created.append("app")
            self._save(config, {"status": "pending", "tenant": tenant, "nonce": nonce,
                                "app_object_id": app["id"], "client_id": app["appId"]}, keep_top=True)
        else:
            result.adopted = True
            body = self._app_body(nonce, graph_sp, exchange_sp)
            self._write("PATCH", f"/applications/{app['id']}",
                        json={k: body[k] for k in ("requiredResourceAccess", "publicClient", "isFallbackPublicClient")})
        result.app_object_id, result.client_id = app["id"], app["appId"]

        sp = self._service_principal(app["appId"])
        if sp is None:
            sp = self._create_sp(app["appId"])
            result.created.append("service principal")
        grants = []
        for resource, names in ((graph_sp, GRAPH_PERMISSIONS), (exchange_sp, EXCHANGE_PERMISSIONS)):
            grants.append(self._grant(sp["id"], resource["id"], names, result))
        self._save(config, {"status": "done", "tenant": tenant, "nonce": nonce, "app_object_id": app["id"],
                            "client_id": app["appId"], "sp_id": sp["id"], "grant_ids": grants},
                   top={"client_id": app["appId"], "tenant": tenant})

    def _create_sp(self, app_id: str) -> dict:
        """A new app takes a moment to be visible to servicePrincipals: wait, at most SP_WAIT_SECONDS."""
        deadline, delay = self.clock() + SP_WAIT_SECONDS, 2.0
        while True:
            try:
                return self._create("/servicePrincipals", {"appId": app_id, "tags": [TAG]},
                                    lambda: self._service_principal(app_id))
            except GraphError as exc:
                if exc.status not in (400, 404) or self.clock() >= deadline:
                    if exc.status in (400, 404):
                        raise SetupError(
                            "P2M-SETUP-PROPAGATION: the app was created but Microsoft has not made it visible yet. "
                            "Run setup again in a few minutes; it continues with the same app") from exc
                    raise
                self.sleep(delay)
                delay = min(delay * 2, 20)

    def _grant(self, client_sp: str, resource_sp: str, names, result: SetupResult) -> str:
        def existing():
            return self._one("/oauth2PermissionGrants",
                             f"clientId eq '{client_sp}' and resourceId eq '{resource_sp}' and consentType eq 'AllPrincipals'",
                             "id,scope")
        found = existing()
        if found is not None:
            have = set((found.get("scope") or "").split())
            if not set(names) <= have:
                self._write("PATCH", f"/oauth2PermissionGrants/{found['id']}",
                            json={"scope": " ".join(sorted(have | set(names)))})
            return found["id"]
        made = self._create("/oauth2PermissionGrants", {"clientId": client_sp, "consentType": "AllPrincipals",
                                                         "resourceId": resource_sp, "scope": " ".join(names)},
                            existing)
        result.created.append("admin consent")
        return made["id"]

    def _save(self, config: dict, record: dict, *, top: dict | None = None, keep_top: bool = False) -> None:
        try:
            write_config(self.config_path, top=({} if keep_top else (top or {})), setup=record)
        except OSError as exc:
            lines = config_lines(record.get("client_id", "<client id>"), record.get("tenant", "<tenant>"))
            extra = "\n".join(f"{k} = {_toml_value(v)}" for k, v in record.items())
            raise SetupError(f"P2M-SETUP-CONFIG: could not write {self.config_path}: {exc}. Put these lines in it "
                             f"yourself:\n{lines}\n\n[setup]\n{extra}") from exc

    # -- remove --

    def remove(self, tenant: str, me: str | None) -> list[str]:
        """Delete the app setup recorded (only if it still carries setup's nonce), and forget it."""
        config = auth_mod.load_config(self.config_path)
        record = read_record(config)
        removed: list[str] = []
        try:
            if not record.get("app_object_id") and not record.get("nonce"):
                return removed
            app = None
            if record.get("app_object_id"):
                try:
                    found = self._get(f"/applications/{record['app_object_id']}", **{"$select": "id,appId,tags"})
                    if f"{NONCE_TAG}{record.get('nonce')}" in (found.get("tags") or []):
                        app = found
                except GraphError as exc:
                    if exc.status != 404:
                        raise
            if app is None and record.get("nonce"):
                app = self._find_by_nonce(record["nonce"])
            if app is not None:
                sp = self._service_principal(app["appId"])
                if sp is not None:
                    self._write("DELETE", f"/servicePrincipals/{sp['id']}")  # its consent goes with it
                    removed.append("service principal and its consent")
                self._write("DELETE", f"/applications/{app['id']}")
                removed.append("app")
                try:
                    self.graph.request("DELETE", f"/directory/deletedItems/{app['id']}")
                    removed.append("app (purged from deleted items)")
                except GraphError:
                    pass  # it stays in deleted items for 30 days; harmless
            top = {}
            if config.get("client_id") and config.get("client_id") == (record.get("client_id") or (app or {}).get("appId")):
                top = {"client_id": None, "tenant": None}
            try:
                write_config(self.config_path, top=top, setup=None)
            except OSError as exc:
                raise SetupError(f"P2M-SETUP-CONFIG: the app was removed but {self.config_path} could not be "
                                 f"updated ({exc}); delete its [setup] table and client_id yourself") from exc
            return removed
        finally:
            try:
                self.settle_cli_grant(me)
            except GraphError:
                pass


def use_app(config_path: Path, client_id: str, tenant: str) -> None:
    """Record an app registered another way (the Azure CLI script, or by hand)."""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", client_id):
        raise SetupError(f"P2M-SETUP-CONFIG: {client_id!r} is not an app (client) ID")
    try:
        write_config(config_path, top={"client_id": client_id, "tenant": tenant}, setup=None)
    except OSError as exc:
        raise SetupError(f"P2M-SETUP-CONFIG: could not write {config_path}: {exc}. Put these lines in it "
                         f"yourself:\n{config_lines(client_id, tenant)}") from exc


def lock_for(config_path: Path) -> FileLock:
    return FileLock(config_path.with_name(config_path.name + ".lock"))


def locked_error(exc: LockHeld) -> SetupError:
    return SetupError(f"P2M-SETUP-LOCKED: another proton2m365 setup is running (pid {exc.holder or '?'}); wait for "
                      "it to finish, then run setup again")


def admin_graph(signin: AdminSignIn, *, sleep: Callable[[float], None] = time.sleep, client=None) -> GraphStore:
    return GraphStore(signin.token, sleep=sleep, client=client)

