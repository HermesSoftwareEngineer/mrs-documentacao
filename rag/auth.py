"""OAuth 2.1 embutido no servidor MCP com login por senha única.

O próprio servidor é o Authorization Server: clientes MCP (Claude, ChatGPT, Cursor...) descobrem os
metadados, registram-se dinamicamente (RFC 7591), abrem /login no navegador, o usuário digita a senha
e o cliente troca o código (PKCE) por access/refresh tokens.

Variáveis de ambiente:
  MCP_PASSWORD     senha da página de login (obrigatória para ativar a autenticação)
  MCP_PUBLIC_URL   URL pública do servidor, ex.: https://mcp.exemplo.com (padrão http://localhost:PORT)
  MCP_STATE_DIR    onde ficam clientes/tokens (SQLite); padrão data/state
  AUTH_MODE        oauth | none (padrão: oauth se MCP_PASSWORD estiver definida)
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import config as C

ACCESS_TTL = 3600  # 1 h
REFRESH_TTL = 30 * 24 * 3600  # 30 dias
CODE_TTL = 300  # 5 min
LOGIN_TTL = 900  # 15 min para digitar a senha
MAX_FAILS = 10  # tentativas erradas por IP a cada 15 min


def auth_mode() -> str:
    mode = os.getenv("AUTH_MODE", "").strip().lower() or ("oauth" if os.getenv("MCP_PASSWORD") else "none")
    if mode not in ("oauth", "none"):
        raise ValueError(f"AUTH_MODE inválido: {mode}")
    if mode == "oauth" and not os.getenv("MCP_PASSWORD"):
        raise RuntimeError("AUTH_MODE=oauth exige MCP_PASSWORD")
    return mode


def public_url() -> str:
    return os.getenv("MCP_PUBLIC_URL", f"http://localhost:{os.getenv('PORT', '8000')}").rstrip("/")


def _h(token: str) -> str:
    """Tokens são guardados só como hash."""
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("CREATE TABLE IF NOT EXISTS kv (kind TEXT, key TEXT, data TEXT, expires REAL, PRIMARY KEY(kind, key))")

    def put(self, kind: str, key: str, data: dict, ttl: float | None = None) -> None:
        exp = time.time() + ttl if ttl else None
        self.con.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?,?)", (kind, key, json.dumps(data), exp))

    def get(self, kind: str, key: str) -> dict | None:
        row = self.con.execute("SELECT data, expires FROM kv WHERE kind=? AND key=?", (kind, key)).fetchone()
        if not row:
            return None
        if row[1] and row[1] < time.time():
            self.delete(kind, key)
            return None
        return json.loads(row[0])

    def delete(self, kind: str, key: str) -> None:
        self.con.execute("DELETE FROM kv WHERE kind=? AND key=?", (kind, key))

    def purge(self) -> None:
        self.con.execute("DELETE FROM kv WHERE expires IS NOT NULL AND expires < ?", (time.time(),))


class PasswordOAuthProvider:
    def __init__(self, password: str, state_dir: Path):
        self.password = password
        self.store = Store(state_dir / "oauth.sqlite")
        self.store.purge()
        self.fails: dict[str, list[float]] = {}

    # --- clientes (registro dinâmico) ---
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put("client", client_info.client_id, client_info.model_dump(mode="json"))

    # --- autorização: manda o navegador para a página de senha ---
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req = secrets.token_urlsafe(24)
        self.store.put("login", req, {"client_id": client.client_id, "client_name": client.client_name,
                                      "params": params.model_dump(mode="json")}, LOGIN_TTL)
        return f"{public_url()}/login?req={req}"

    def complete_login(self, req: str) -> str | None:
        pending = self.store.get("login", req)
        if not pending:
            return None
        self.store.delete("login", req)
        p = AuthorizationParams.model_validate(pending["params"])
        code = secrets.token_urlsafe(32)
        self.store.put("code", _h(code), AuthorizationCode(
            code="", scopes=p.scopes or [], expires_at=time.time() + CODE_TTL, client_id=pending["client_id"],
            code_challenge=p.code_challenge, redirect_uri=p.redirect_uri,
            redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,
            resource=p.resource, subject="usuario",
        ).model_dump(mode="json"), CODE_TTL)
        return construct_redirect_uri(str(p.redirect_uri), code=code, state=p.state)

    # --- códigos e tokens ---
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        data = self.store.get("code", _h(authorization_code))
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate({**data, "code": authorization_code})

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, subject: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        self.store.put("access", _h(access), AccessToken(
            token="", client_id=client_id, scopes=scopes, expires_at=now + ACCESS_TTL,
            resource=resource, subject=subject).model_dump(mode="json"), ACCESS_TTL)
        self.store.put("refresh", _h(refresh), RefreshToken(
            token="", client_id=client_id, scopes=scopes, expires_at=now + REFRESH_TTL,
            resource=resource, subject=subject).model_dump(mode="json"), REFRESH_TTL)
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes) or None,
                          refresh_token=refresh)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        if not self.store.get("code", _h(authorization_code.code)):
            raise TokenError("invalid_grant", "código já utilizado ou expirado")
        self.store.delete("code", _h(authorization_code.code))  # uso único
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource,
                           authorization_code.subject)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        data = self.store.get("refresh", _h(refresh_token))
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken.model_validate({**data, "token": refresh_token})

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        self.store.delete("refresh", _h(refresh_token.token))  # rotação
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource,
                           refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self.store.get("access", _h(token))
        return AccessToken.model_validate({**data, "token": token}) if data else None

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def revoke_token(self, token) -> None:
        self.store.delete("access", _h(token.token))
        self.store.delete("refresh", _h(token.token))

    # --- página de login ---
    def _blocked(self, ip: str) -> bool:
        now = time.time()
        self.fails[ip] = [t for t in self.fails.get(ip, []) if now - t < LOGIN_TTL]
        return len(self.fails[ip]) >= MAX_FAILS

    async def login_page(self, request: Request) -> Response:
        req = request.query_params.get("req", "")
        pending = self.store.get("login", req)
        if not pending:
            return HTMLResponse(_page("Link expirado", "<p>Este pedido de acesso expirou. Volte ao aplicativo e "
                                      "conecte de novo.</p>"), status_code=400)
        error = ""
        if request.method == "POST":
            ip = request.client.host if request.client else "?"
            if self._blocked(ip):
                return HTMLResponse(_page("Muitas tentativas", "<p>Aguarde alguns minutos e tente de novo.</p>"),
                                    status_code=429)
            form = await request.form()
            if hmac.compare_digest(str(form.get("password", "")).encode(), self.password.encode()):
                return RedirectResponse(self.complete_login(req), status_code=302)
            self.fails.setdefault(ip, []).append(time.time())
            await anyio.sleep(1)
            error = '<p class="err">Senha incorreta.</p>'
        app = html.escape(pending.get("client_name") or "Um aplicativo")
        body = f"""<p><b>{app}</b> quer acessar a base de requisitos MRS para fornecedores.</p>
<form method="post">
  <label for="pw">Senha</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
  {error}
  <button type="submit">Entrar</button>
</form>"""
        return HTMLResponse(_page("Acesso à base MRS", body))


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#f4f5f7;--card:#fff;--fg:#1d2433;--muted:#5b6475;--accent:#0b5cad;--err:#b42318;--line:#d5d9e0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--card:#1b2029;--fg:#e8ebf0;--muted:#a2aab8;--accent:#5aa2f0;--err:#f97066;--line:#333a47}}}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
color:var(--fg);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px}}
main{{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px}}
h1{{font-size:20px;margin:0 0 12px}}p{{color:var(--muted);margin:0 0 20px}}label{{display:block;font-weight:600;margin-bottom:6px}}
input{{width:100%;padding:10px 12px;font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:8px}}
button{{margin-top:16px;width:100%;padding:11px;font:inherit;font-weight:600;color:#fff;background:var(--accent);border:0;border-radius:8px;cursor:pointer}}
.err{{color:var(--err);margin:10px 0 0}}
</style></head><body><main><h1>{title}</h1>{body}</main></body></html>"""


def oauth_setup() -> tuple[AuthSettings, PasswordOAuthProvider]:
    base = public_url()
    provider = PasswordOAuthProvider(os.environ["MCP_PASSWORD"], Path(os.getenv("MCP_STATE_DIR", C.DATA_DIR / "state")))
    settings = AuthSettings(
        issuer_url=base,
        resource_server_url=f"{base}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=False,
    )
    return settings, provider
