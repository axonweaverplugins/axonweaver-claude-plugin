#!/usr/bin/env python3
"""The AxonWeaver plugin's start of a session: the platform's guide for this person's agent.

    axon_bootstrap.py hook     the SessionStart hook: fetch GET <platform>/api/agent/bootstrap with the personal
                               agent key, keep it next to the plugin's data, answer the hook with the short part
                               (a SessionStart hook carries at most 10 000 characters) and where the full guide lies
    axon_bootstrap.py status   what was fetched last: who, roles, version, when, MCP servers (no key needed)

Only the standard library. The key comes from the plugin's secure settings (Claude Code hands it to the hook as
CLAUDE_PLUGIN_OPTION_AGENT_KEY); it is sent to the platform and never written anywhere. A failed fetch never blocks
the session: the last guide is used, marked as such.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.2.0"
DEFAULT_URL = "https://admin.axonweaver.ai"
LIMIT = 9500  # a SessionStart hook's context is cut at 10 000 characters


def data_dir() -> Path:
    # one folder for the hook, the commands and the MCP headers helper (the commands' shell has no CLAUDE_PLUGIN_DATA)
    folder = Path(os.environ.get("AXON_PLUGIN_HOME") or Path.home() / ".claude" / "axonweaver")
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    return folder


def keep(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def fetch(url: str, key: str, person_token: str = "") -> dict:
    headers = {"Accept": "application/json", "User-Agent": f"axonweaver-claude-plugin/{VERSION}"}
    if person_token:
        headers["Authorization"] = f"Bearer {person_token}"  # the person signed in through Keycloak (/axonweaver:login)
    else:
        headers["X-Axon-Key"] = key
    request = urllib.request.Request(url.rstrip("/") + "/api/agent/bootstrap", headers=headers)
    with urllib.request.urlopen(request, timeout=8) as answer:
        return json.loads(answer.read().decode("utf-8"))


def why(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return "платформа не приняла вход агента (401) — войди заново (/axonweaver:login) или проверь ключ в настройках плагина"
        return f"платформа ответила {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"платформа недоступна ({exc.reason})"
    return f"{type(exc).__name__}: {exc}"


def context(bundle: dict, guide_path: Path, note: str) -> str:
    tail = (f"\n**Полное руководство** сохранено в `{guide_path}` — прочитай его (Read) перед первой задачей на платформе. "
            "Обновить посреди сессии: MCP-инструмент `agent_guide` сервера `axonweaver` (part=full).")
    text = (note + "\n\n" if note else "") + str(bundle.get("brief") or "").strip() + "\n" + tail
    return text[:LIMIT]


def hook() -> int:
    try:
        sys.stdin.read()  # the hook's input (session id, source); not needed
    except Exception:  # noqa: BLE001
        pass
    folder = data_dir()
    cache, guide_path, meta_path = folder / "bootstrap.json", folder / "guide.md", folder / "meta.json"
    key = (os.environ.get("CLAUDE_PLUGIN_OPTION_AGENT_KEY") or os.environ.get("AXON_AGENT_KEY") or "").strip()
    url = (os.environ.get("CLAUDE_PLUGIN_OPTION_PLATFORM_URL") or DEFAULT_URL).strip()
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    note = ""
    # 0.2.1: the MCP header is `Bearer <mcp_bearer>`; a value that already says «Bearer …» makes «Bearer Bearer …»
    # and the platform answers 401 (vitek1, 2026-10-22). The headersHelper cannot read the option — the hook can.
    bearer = (os.environ.get("CLAUDE_PLUGIN_OPTION_MCP_BEARER") or "").strip()
    warnings = []
    if bearer.lower().startswith("bearer"):
        warnings.append("⚠ В настройке плагина «Служебный ключ MCP» (mcp_bearer) лишнее слово «Bearer» — MCP платформы "
                        "ответит 401. Попроси человека: /plugin configure axonweaver@axonweaver → поле mcp_bearer очистить "
                        "(при входе своей учётной записью) или оставить только сам ключ.")
    try:
        person_token = ""
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import axon_auth
            person_token = axon_auth.access_token()
        except Exception:  # noqa: BLE001 — no Keycloak sign-in: the key decides
            person_token = ""
        if not key and not person_token:
            raise RuntimeError("агент не вошёл: /axonweaver:login (своя учётная запись AxonWeaver) или ключ агента в "
                               "/plugin configure axonweaver@axonweaver (его выдаёт владелец платформы)")
        bundle = fetch(url, key, person_token)
        keep(cache, json.dumps(bundle, ensure_ascii=False))
        keep(guide_path, str(bundle.get("guide") or ""))
        if person_token and bearer:
            warnings.append("Плагин вошёл учётной записью Keycloak — служебный ключ MCP (mcp_bearer) не нужен; "
                            "его можно очистить в /plugin configure axonweaver@axonweaver.")
        if meta.get("version") and meta["version"] != bundle.get("version"):
            note = f"Руководство агента обновилось на платформе: {meta['version']} → {bundle.get('version')}."
        keep(meta_path, json.dumps({"version": bundle.get("version"), "fetched_at": time.time(), "url": url}))
    except Exception as exc:  # noqa: BLE001 — the session goes on with what was fetched before
        reason = why(exc) if not isinstance(exc, RuntimeError) else str(exc)
        if cache.exists():
            bundle = json.loads(cache.read_text())
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.get("fetched_at") or 0))
            note = f"⚠ Плагин AxonWeaver не обновил руководство: {reason}. Ниже — версия {bundle.get('version')} от {when}."
        else:
            text = (f"⚠ Плагин AxonWeaver установлен, но руководство агента не получено: {reason}. Скажи об этом человеку. "
                    "До исправления держись навыка `axonweaver-basics` этого плагина.")
            if warnings:
                text = "\n".join(warnings) + "\n" + text
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
                             ensure_ascii=False))
            return 0
    if warnings:
        note = "\n".join(warnings + ([note] if note else []))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": context(bundle, guide_path, note)}},
                     ensure_ascii=False))
    return 0


def status() -> int:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import axon_auth
        axon_auth.whoami()
    except Exception:  # noqa: BLE001
        pass
    folder = data_dir()
    cache, meta_path = folder / "bootstrap.json", folder / "meta.json"
    if not cache.exists():
        print("Руководство ещё не получено: оно приходит при старте сессии с ключом агента в настройках плагина.")
        return 0
    bundle, meta = json.loads(cache.read_text()), (json.loads(meta_path.read_text()) if meta_path.exists() else {})
    you = bundle.get("you") or {}
    print(f"Кто: {you.get('name')} ({you.get('kind')}), ранг {you.get('rank')}, суперадмин: {you.get('superadmin')}")
    print("Роли: " + ", ".join(r for r in bundle.get("roles") or [] if r != "all"))
    if you.get("projects"):
        print("Проекты: " + "; ".join(f"{p}: {', '.join(r)}" for p, r in you["projects"].items()))
    print(f"Руководство {bundle.get('version')} (платформа {bundle.get('platform_version')}), получено "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(meta.get('fetched_at') or 0))} с {meta.get('url')}")
    print(f"Полное руководство: {folder / 'guide.md'}")
    print("MCP-серверы ролей:")
    for s in bundle.get("mcp_servers") or []:
        print(f"  - {s.get('name')}: {s.get('url') or 'локальный'} — {s.get('note') or ''}")
    print(f"Навыков рабочих мест: {len(bundle.get('skills') or [])}")
    return 0


if __name__ == "__main__":
    sys.exit({"hook": hook, "status": status}.get((sys.argv[1:] or ["status"])[0], status)())
