"""`setup` against a fake tenant: Graph's applications, servicePrincipals and oauth2PermissionGrants."""

from __future__ import annotations

import itertools
import json
import re
import tomllib
from urllib.parse import parse_qs

import httpx
import pytest

from proton2m365 import auth as auth_mod
from proton2m365 import graph_store as gs
from proton2m365 import setup as su

ME = "admin-oid"


def scopes(*names):
    return [{"id": f"id-{n}", "value": n} for n in names]


class FakeTenant:
    def __init__(self):
        self.ids = (f"obj-{i}" for i in itertools.count(1))
        self.apps: dict[str, dict] = {}
        self.sps: dict[str, dict] = {
            "sp-graph": {"id": "sp-graph", "appId": su.GRAPH_APP,
                         "oauth2PermissionScopes": scopes(*su.GRAPH_PERMISSIONS, "User.Read")},
            "sp-exo": {"id": "sp-exo", "appId": su.EXCHANGE_APP,
                       "oauth2PermissionScopes": scopes(*su.EXCHANGE_PERMISSIONS)},
            "sp-cli": {"id": "sp-cli", "appId": su.GRAPH_CLI, "oauth2PermissionScopes": []},
        }
        self.grants: dict[str, dict] = {"g-cli": {
            "id": "g-cli", "clientId": "sp-cli", "consentType": "AllPrincipals", "resourceId": "sp-graph",
            "scope": " Application.ReadWrite.All DelegatedPermissionGrant.ReadWrite.All offline_access openid profile"}}
        self.purged: list[str] = []
        self.forbid: set[tuple[str, str]] = set()   # (method, path prefix) answered with 403
        self.lose_reply: set[str] = set()           # paths whose next POST succeeds but answers 504
        self.invisible_for = 0                      # SP creates for a new app answered 400 this many times
        self.log: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1.0")
        method = request.method
        self.log.append((method, path))
        if any(method == m and path.startswith(p) for m, p in self.forbid):
            return httpx.Response(403, json={"error": {"code": "Authorization_RequestDenied", "message": "no"}})
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        body = json.loads(request.content) if request.content else {}
        flt = q.get("$filter", "")

        if path == "/applications" and method == "GET":
            if m := re.match(r"displayName eq '(.+)'", flt):
                items = [a for a in self.apps.values() if a["displayName"] == m.group(1)]
            elif m := re.match(r"tags/any\(t:t eq '(.+)'\)", flt):
                items = [a for a in self.apps.values() if m.group(1) in a.get("tags", [])]
            else:
                items = list(self.apps.values())
            return httpx.Response(200, json={"value": items})
        if path == "/applications" and method == "POST":
            oid = next(self.ids)
            app = {**body, "id": oid, "appId": f"app-{oid}"}
            self.apps[oid] = app
            if path in self.lose_reply:
                self.lose_reply.discard(path)
                return httpx.Response(504, json={"error": {"code": "GatewayTimeout", "message": "lost"}})
            return httpx.Response(201, json=app)
        if m := re.fullmatch(r"/applications/([^/]+)", path):
            app = self.apps.get(m.group(1))
            if app is None:
                return httpx.Response(404, json={"error": {"code": "Request_ResourceNotFound", "message": "gone"}})
            if method == "GET":
                return httpx.Response(200, json=app)
            if method == "PATCH":
                app.update(body)
                return httpx.Response(204)
            if method == "DELETE":
                del self.apps[app["id"]]
                return httpx.Response(204)
        if m := re.fullmatch(r"/directory/deletedItems/([^/]+)", path):
            self.purged.append(m.group(1))
            return httpx.Response(204)

        if path == "/servicePrincipals" and method == "GET":
            m = re.match(r"appId eq '(.+)'", flt)
            return httpx.Response(200, json={"value": [s for s in self.sps.values() if s["appId"] == m.group(1)]})
        if path == "/servicePrincipals" and method == "POST":
            if body["appId"].startswith("app-") and self.invisible_for:
                self.invisible_for -= 1
                return httpx.Response(400, json={"error": {"code": "Request_BadRequest",
                                                           "message": "The appId of the service principal does not reference a valid application object."}})
            sid = f"sp-{next(self.ids)}"
            sp = {"id": sid, "appId": body["appId"], "oauth2PermissionScopes": []}
            self.sps[sid] = sp
            return httpx.Response(201, json=sp)
        if m := re.fullmatch(r"/servicePrincipals/([^/]+)", path):
            sid = m.group(1)
            self.sps.pop(sid, None)
            self.grants = {k: g for k, g in self.grants.items() if g["clientId"] != sid}
            return httpx.Response(204)

        if path == "/oauth2PermissionGrants" and method == "GET":
            terms = dict(re.findall(r"(\w+) eq '([^']+)'", flt))
            items = [g for g in self.grants.values() if all(g.get(k) == v for k, v in terms.items())]
            return httpx.Response(200, json={"value": items})
        if path == "/oauth2PermissionGrants" and method == "POST":
            gid = f"g-{next(self.ids)}"
            self.grants[gid] = {**body, "id": gid}
            if path in self.lose_reply:
                self.lose_reply.discard(path)
                return httpx.Response(504, json={"error": {"code": "GatewayTimeout", "message": "lost"}})
            return httpx.Response(201, json=self.grants[gid])
        if m := re.fullmatch(r"/oauth2PermissionGrants/([^/]+)", path):
            if method == "PATCH":
                self.grants[m.group(1)].update(body)
            else:
                self.grants.pop(m.group(1), None)
            return httpx.Response(204)
        return httpx.Response(400, json={"error": {"code": "BadRoute", "message": f"{method} {path}"}})

    def ours(self) -> list[dict]:
        return [a for a in self.apps.values() if a["displayName"] == su.APP_NAME
                and any(t.startswith(su.NONCE_TAG) for t in a.get("tags", []))]


@pytest.fixture
def tenant():
    return FakeTenant()


@pytest.fixture
def config(tmp_path):
    return tmp_path / "cfg" / "config.toml"


def make(tenant, config, tmp_path, **kwargs):
    graph = gs.GraphStore(lambda: "tok", sleep=lambda s: None,
                          client=httpx.Client(base_url=gs.GRAPH, transport=httpx.MockTransport(tenant.handler)))
    t = itertools.count()
    return su.Setup(graph, config, say=lambda m: None, sleep=lambda s: None, clock=lambda: next(t),
                    script_dir=tmp_path / "scripts", tenant_hint="tid-1", **kwargs)


def read(config):
    return tomllib.loads(config.read_text())


def test_fresh_setup_registers_the_app_and_writes_config(tenant, config, tmp_path):
    r = make(tenant, config, tmp_path).run("tid-1", ME)
    (app,) = tenant.ours()
    assert app["isFallbackPublicClient"] and app["publicClient"]["redirectUris"] == ["http://localhost"]
    graph_access, exo_access = app["requiredResourceAccess"]
    assert {a["id"] for a in graph_access["resourceAccess"]} == {f"id-{n}" for n in su.GRAPH_PERMISSIONS}
    assert [a["id"] for a in exo_access["resourceAccess"]] == ["id-IMAP.AccessAsUser.All"]
    sp = next(s for s in tenant.sps.values() if s["appId"] == app["appId"])
    grants = {g["resourceId"]: g["scope"] for g in tenant.grants.values() if g["clientId"] == sp["id"]}
    assert grants == {"sp-graph": " ".join(su.GRAPH_PERMISSIONS), "sp-exo": "IMAP.AccessAsUser.All"}
    cfg = read(config)
    assert cfg["client_id"] == app["appId"] and cfg["tenant"] == "tid-1"
    assert cfg["setup"]["status"] == "done" and cfg["setup"]["app_object_id"] == app["id"]
    assert r.cli_grant == "revoked" and "g-cli" not in tenant.grants   # setup's own consent tidied away (D22)


def test_setup_never_touches_the_mail_token_cache(tenant, config, tmp_path, monkeypatch):
    make(tenant, config, tmp_path).run("tid-1", ME)
    assert not auth_mod.DEFAULT_CACHE.exists()


def test_rerun_updates_the_recorded_app_and_creates_no_second(tenant, config, tmp_path):
    make(tenant, config, tmp_path).run("tid-1", ME)
    r = make(tenant, config, tmp_path).run("tid-1", ME)
    assert r.adopted and r.created == [] and len(tenant.ours()) == 1
    assert ("PATCH", f"/applications/{tenant.ours()[0]['id']}") in tenant.log


def test_interrupted_after_create_is_picked_up_by_the_nonce(tenant, config, tmp_path):
    s = make(tenant, config, tmp_path)
    tenant.forbid.add(("POST", "/servicePrincipals"))                 # stops right after the app was created
    with pytest.raises(su.SetupBlocked):
        s.run("tid-1", ME)
    tenant.forbid.clear()
    record = read(config)["setup"]
    assert record["status"] == "pending" and record["nonce"]
    make(tenant, config, tmp_path).run("tid-1", ME)
    assert len(tenant.ours()) == 1 and read(config)["setup"]["status"] == "done"


def test_interrupted_before_the_record_names_the_app_finds_it_by_tag(tenant, config, tmp_path):
    s = make(tenant, config, tmp_path)
    s.run("tid-1", ME)
    record = read(config)["setup"]
    su.write_config(config, top={}, setup={"status": "pending", "tenant": "tid-1", "nonce": record["nonce"]})
    make(tenant, config, tmp_path).run("tid-1", ME)
    assert len(tenant.ours()) == 1


def test_a_lost_reply_on_create_does_not_make_a_second_app_or_grant(tenant, config, tmp_path):
    tenant.lose_reply = {"/applications", "/oauth2PermissionGrants"}
    make(tenant, config, tmp_path).run("tid-1", ME)
    assert len(tenant.ours()) == 1
    sp = next(s for s in tenant.sps.values() if s["appId"] == tenant.ours()[0]["appId"])
    assert len([g for g in tenant.grants.values() if g["clientId"] == sp["id"]]) == 2


def test_a_new_app_not_yet_visible_is_waited_for(tenant, config, tmp_path):
    tenant.invisible_for = 2
    make(tenant, config, tmp_path).run("tid-1", ME)
    assert read(config)["setup"]["status"] == "done"


def test_an_app_that_never_becomes_visible_says_run_again(tenant, config, tmp_path):
    tenant.invisible_for = 10**6
    with pytest.raises(su.SetupError, match="P2M-SETUP-PROPAGATION"):
        make(tenant, config, tmp_path).run("tid-1", ME)


def test_a_same_named_app_setup_did_not_create_is_left_alone(tenant, config, tmp_path):
    tenant.apps["foreign"] = {"id": "foreign", "appId": "app-foreign", "displayName": su.APP_NAME, "tags": []}
    r = make(tenant, config, tmp_path).run("tid-1", ME)
    assert r.foreign_apps == 1 and tenant.apps["foreign"] == {"id": "foreign", "appId": "app-foreign",
                                                               "displayName": su.APP_NAME, "tags": []}
    make(tenant, config, tmp_path).remove("tid-1", ME)
    assert "foreign" in tenant.apps


def test_cli_grant_with_other_permissions_is_kept(tenant, config, tmp_path):
    tenant.grants["g-cli"]["scope"] = "Application.ReadWrite.All DelegatedPermissionGrant.ReadWrite.All User.Read.All"
    r = make(tenant, config, tmp_path).run("tid-1", ME)
    assert r.cli_grant == "kept" and "g-cli" in tenant.grants


def test_dry_run_creates_nothing(tenant, config, tmp_path):
    r = make(tenant, config, tmp_path).run("tid-1", ME, dry_run=True)
    assert r.plan and not tenant.ours() and not config.exists()
    assert not [x for x in tenant.log if x[0] in ("POST", "PATCH")]


def test_missing_role_writes_the_script_and_blocks(tenant, config, tmp_path):
    tenant.forbid.add(("POST", "/applications"))
    with pytest.raises(su.SetupBlocked, match="P2M-SETUP-ROLE") as exc:
        make(tenant, config, tmp_path).run("tid-1", ME)
    script = exc.value.script
    assert script.exists() and "az ad app create" in script.read_text()
    assert str(script) in str(exc.value)


def test_missing_exchange_service_principal_is_added(tenant, config, tmp_path):
    del tenant.sps["sp-exo"]
    real = tenant.handler

    def handler(request):
        response = real(request)
        if request.method == "POST" and json.loads(request.content).get("appId") == su.EXCHANGE_APP:
            sp = next(s for s in tenant.sps.values() if s["appId"] == su.EXCHANGE_APP)
            sp["oauth2PermissionScopes"] = scopes(*su.EXCHANGE_PERMISSIONS)
        return response
    tenant.handler = handler
    r = make(tenant, config, tmp_path).run("tid-1", ME)
    assert "Exchange Online service principal" in r.created


def test_config_write_failure_prints_the_lines(tenant, config, tmp_path, monkeypatch):
    def fail(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(su, "write_config", fail)
    with pytest.raises(su.SetupError, match="P2M-SETUP-CONFIG") as exc:
        make(tenant, config, tmp_path).run("tid-1", ME)
    assert "[setup]" in str(exc.value) and "nonce" in str(exc.value)


def test_remove_deletes_only_the_recorded_app_and_forgets_it(tenant, config, tmp_path):
    config.parent.mkdir(parents=True)
    config.write_text('# my settings\n[map.folders]\n"Projects" = "Clients"\n')
    make(tenant, config, tmp_path).run("tid-1", ME)
    app_id = tenant.ours()[0]["id"]
    removed = make(tenant, config, tmp_path).remove("tid-1", ME)
    assert removed and not tenant.ours() and app_id in tenant.purged
    cfg = read(config)
    assert "setup" not in cfg and "client_id" not in cfg and cfg["map"]["folders"] == {"Projects": "Clients"}
    assert "# my settings" in config.read_text()


def test_remove_with_nothing_recorded_does_nothing(tenant, config, tmp_path):
    assert make(tenant, config, tmp_path).remove("tid-1", ME) == []


def test_config_text_keeps_everything_else():
    before = 'client_id = "old"\n# comment\naccount = "a@b"\n\n[map.labels]\n"Work" = "Office"\n\n[setup]\nnonce = "x"\n'
    after = su.config_text(before, top={"client_id": "new", "tenant": "t"}, setup={"nonce": "y", "grant_ids": ["a", "b"]})
    cfg = tomllib.loads(after)
    assert cfg["client_id"] == "new" and cfg["tenant"] == "t" and cfg["account"] == "a@b"
    assert cfg["map"]["labels"] == {"Work": "Office"} and cfg["setup"] == {"nonce": "y", "grant_ids": ["a", "b"]}
    assert after.count("[setup]") == 1 and "# comment" in after


def test_fallback_scripts_name_every_permission():
    for windows in (False, True):
        text = su.fallback_script("contoso.onmicrosoft.com", windows=windows)
        for name in (*su.GRAPH_PERMISSIONS, *su.EXCHANGE_PERMISSIONS):
            assert su.PERMISSION_IDS[name] in text
        assert "setup --use-app" in text and "contoso.onmicrosoft.com" in text


class FakeMsal:
    def __init__(self, result):
        self.result = result

    def acquire_token_interactive(self, scopes, **kw):
        return self.result

    def initiate_device_flow(self, scopes):
        return {"user_code": "X", "message": "go"}

    def acquire_token_by_device_flow(self, flow):
        return self.result

    def get_accounts(self):
        return [{"home_account_id": "a"}]

    def acquire_token_silent(self, scopes, account):
        return None


def test_personal_account_gets_the_route_b_message(tmp_path):
    signin = su.AdminSignIn(app=FakeMsal({"access_token": "t", "id_token_claims": {"tid": auth_mod.CONSUMER_TENANT}}),
                            say=lambda m: None, is_headless=lambda: False)
    with pytest.raises(su.SetupError, match="P2M-SETUP-PERSONAL"):
        signin.sign_in()


def test_blocked_graph_cli_writes_the_script(tmp_path):
    signin = su.AdminSignIn(app=FakeMsal({"error": "invalid_grant", "error_description": "AADSTS65001: blocked"}),
                            say=lambda m: None, is_headless=lambda: False, script_dir=tmp_path)
    with pytest.raises(su.SetupBlocked, match="P2M-SETUP-BLOCKED") as exc:
        signin.sign_in()
    assert exc.value.script.exists()


def test_cli_use_app_records_the_app(capsys):
    from proton2m365 import cli

    code = cli.main(["setup", "--use-app", "12345678-1234-1234-1234-123456789012", "--tenant", "contoso.com"])
    assert code == 0
    cfg = tomllib.loads(auth_mod.DEFAULT_CONFIG.read_text())
    assert cfg["client_id"] == "12345678-1234-1234-1234-123456789012" and cfg["tenant"] == "contoso.com"
    assert cli.main(["setup", "--use-app", "nonsense", "--tenant", "contoso.com"]) == 2
