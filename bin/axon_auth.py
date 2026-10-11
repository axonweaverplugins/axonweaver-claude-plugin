#!/usr/bin/env python3
"""The agent signs in as its person: Keycloak's device code (realm axon, public client axonweaver-mcp), 2026-10-11.

    axon_auth.py login     start the sign-in: print the link and the code, keep polling Keycloak in the background
    axon_auth.py headers   the MCP connection's headersHelper: {"Authorization": "Bearer <fresh access token>"}, or {}
    axon_auth.py logout    end the agent's Keycloak session and forget its tokens
    axon_auth.py whoami    who the stored sign-in is (no token printed)

The offline refresh token lives in ~/.claude/axonweaver/keycloak.json (folder 700, file 600) on the person's machine;
access tokens live minutes and are refreshed on demand. No token is ever printed. Standard library only.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ISSUER = os.environ.get("AXON_KC_ISSUER", "https://kc.axonweaver.ai/realms/axon").rstrip("/")
CLIENT = os.environ.get("AXON_KC_CLIENT", "axonweaver-mcp")
HOME = Path(os.environ.get("AXON_PLUGIN_HOME") or Path.home() / ".claude" / "axonweaver")
STORE = HOME / "keycloak.json"
PENDING = HOME / "keycloak-pending.json"


def home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    try:
        HOME.chmod(0o700)
    except OSError:
        pass
    return HOME


def keep(path: Path, data: dict) -> None:
    home()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def post(url: str, fields: dict, timeout: float = 6) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return json.loads(answer.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return {"error": "http", **json.loads(exc.read().decode() or "{}"), "status": exc.code}
        except ValueError:
            return {"error": "http", "status": exc.code}


def claims(token: str) -> dict:
    """The token's payload, unverified — only to show who signed in and when it expires."""
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:  # noqa: BLE001
        return {}


def save_tokens(answer: dict, old: dict | None = None) -> dict:
    access = answer.get("access_token") or ""
    info = claims(access)
    data = {"issuer": ISSUER, "client": CLIENT,
            "refresh_token": answer.get("refresh_token") or (old or {}).get("refresh_token"),
            "access_token": access, "access_exp": info.get("exp") or (time.time() + int(answer.get("expires_in") or 60)),
            "user": info.get("preferred_username") or (old or {}).get("user"),
            "roles": (info.get("realm_access") or {}).get("roles") or (old or {}).get("roles") or [],
            "signed_in_at": (old or {}).get("signed_in_at") or time.time()}
    keep(STORE, data)
    return data


def access_token() -> str:
    """A valid access token of the stored sign-in, refreshed when it is about to expire; '' when there is none."""
    if not STORE.exists():
        return ""
    data = json.loads(STORE.read_text())
    if data.get("access_token") and float(data.get("access_exp") or 0) - time.time() > 30:
        return data["access_token"]
    if not data.get("refresh_token"):
        return ""
    answer = post(f"{ISSUER}/protocol/openid-connect/token", {
        "grant_type": "refresh_token", "refresh_token": data["refresh_token"], "client_id": CLIENT})
    if answer.get("access_token"):
        return save_tokens(answer, data)["access_token"]
    if answer.get("error") in ("invalid_grant", "unauthorized_client"):  # revoked or expired: sign in again
        STORE.unlink(missing_ok=True)
    return ""


def login() -> int:
    answer = post(f"{ISSUER}/protocol/openid-connect/auth/device", {"client_id": CLIENT, "scope": "openid offline_access"})
    if not answer.get("device_code"):
        print(f"Keycloak не начал вход ({answer.get('error_description') or answer.get('error') or answer}). "
              "Скорее всего, клиент axonweaver-mcp ещё не заведён в realm axon — скажи владельцу платформы.")
        return 0
    keep(PENDING, {"device_code": answer["device_code"], "interval": int(answer.get("interval") or 5),
                   "until": time.time() + int(answer.get("expires_in") or 600)})
    subprocess.Popen([sys.executable, __file__, "poll"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    link = answer.get("verification_uri_complete") or answer.get("verification_uri")
    minutes = int(answer.get("expires_in") or 600) // 60
    print(f"Вход агента в AxonWeaver через Keycloak.\n\nОткрой ссылку: {link}\nКод для проверки: {answer.get('user_code')}\n\n"
          f"Войди своей учётной записью AxonWeaver и подтверди код (ссылка действует {minutes} мин). Плагин ждёт подтверждения "
          "в фоне; после него начни новую сессию — или проверь /axonweaver:status.")
    return 0


def poll() -> int:
    """The background half of login: ask Keycloak until the person confirmed the code (or it expired)."""
    if not PENDING.exists():
        return 0
    pending = json.loads(PENDING.read_text())
    interval = pending.get("interval", 5)
    while time.time() < pending["until"]:
        time.sleep(interval)
        try:
            answer = post(f"{ISSUER}/protocol/openid-connect/token", {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": pending["device_code"], "client_id": CLIENT})
        except Exception:  # noqa: BLE001 — the network blinked: ask again
            continue
        if answer.get("access_token"):
            save_tokens(answer)
            break
        error = answer.get("error")
        if error == "slow_down":
            interval += 5
        elif error in ("authorization_pending", "http"):
            continue  # not confirmed yet (or a bare error page): keep asking until the code expires
        else:
            break  # access_denied, expired_token: the person has to start again
    PENDING.unlink(missing_ok=True)
    return 0


def headers() -> int:
    try:
        token = access_token()
    except Exception:  # noqa: BLE001 — the static headers (keys) stay in force
        token = ""
    print(json.dumps({"Authorization": f"Bearer {token}"} if token else {}))
    return 0


def logout() -> int:
    if not STORE.exists():
        print("Вход через Keycloak не выполнен — выходить не из чего.")
        return 0
    data = json.loads(STORE.read_text())
    if data.get("refresh_token"):
        post(f"{ISSUER}/protocol/openid-connect/logout", {"client_id": CLIENT, "refresh_token": data["refresh_token"]})
    STORE.unlink(missing_ok=True)
    print(f"Агент вышел из Keycloak ({data.get('user')}); токены на этом компьютере удалены.")
    return 0


def whoami() -> int:
    if not STORE.exists():
        print("Вход через Keycloak: не выполнен (/axonweaver:login).")
        return 0
    data = json.loads(STORE.read_text())
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(data.get("signed_in_at") or 0))
    roles = ", ".join(r for r in data.get("roles") or [] if not r.startswith("default-roles") and r not in ("offline_access", "uma_authorization"))
    print(f"Вход через Keycloak: {data.get('user')} (роли realm: {roles or '—'}), с {since}.")
    return 0


if __name__ == "__main__":
    command = (sys.argv[1:] or ["whoami"])[0]
    sys.exit({"login": login, "poll": poll, "headers": headers, "logout": logout, "whoami": whoami}.get(command, whoami)())
