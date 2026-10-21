"""Configuration store: /etc/xenproxy/config.json (mode 0600)."""
from __future__ import annotations

import json
import os

CONF_DIR = "/etc/xenproxy"
CONF_FILE = os.path.join(CONF_DIR, "config.json")
KEY_DIR = os.path.join(CONF_DIR, "keys")

DEFAULTS = {
    "servers": [],
    "settings": {
        "theme": "tokyo-night",
        "border": "rounded",
        "mouse": True,
        "banner": True,
        "ambient": True,
        "default_storage": "",
        "default_bridge": "",
        "check_updates": True,
        "verify_transfers": True,
        "guest_prep": True,
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    try:
        with open(CONF_FILE, encoding="utf-8") as fh:
            return _merge(DEFAULTS, json.load(fh))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULTS))


def save(cfg: dict) -> None:
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    tmp = CONF_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF_FILE)
    try:
        os.chmod(CONF_DIR, 0o700)
    except OSError:
        pass


def exists() -> bool:
    return os.path.exists(CONF_FILE)


def servers(cfg: dict) -> list[dict]:
    return cfg.get("servers", [])


def find_server(cfg: dict, name: str) -> dict | None:
    for s in cfg.get("servers", []):
        if s.get("name") == name:
            return s
    return None


def upsert_server(cfg: dict, server: dict) -> dict:
    out = [s for s in cfg.get("servers", []) if s.get("name") != server.get("name")]
    out.append(server)
    cfg["servers"] = sorted(out, key=lambda s: s.get("name", ""))
    return cfg


def remove_server(cfg: dict, name: str) -> dict:
    cfg["servers"] = [s for s in cfg.get("servers", []) if s.get("name") != name]
    return cfg


def setting(cfg: dict, key: str, default=None):
    return cfg.get("settings", {}).get(key, default)


def set_setting(cfg: dict, key: str, value) -> dict:
    cfg.setdefault("settings", {})[key] = value
    return cfg


def generate_keypair(name: str) -> tuple[str, str]:
    """Create a dedicated ed25519 keypair for one Xen host."""
    import subprocess
    os.makedirs(KEY_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(KEY_DIR, f"{name}_ed25519")
    if not os.path.exists(path):
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", path,
             "-C", f"xenproxy@{name}"],
            check=True, capture_output=True, timeout=60)
        os.chmod(path, 0o600)
    with open(path + ".pub", encoding="utf-8") as fh:
        return path, fh.read().strip()
