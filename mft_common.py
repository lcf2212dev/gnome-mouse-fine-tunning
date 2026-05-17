"""Compartilhado entre GUI e daemon: caminhos, presets, devices.json, math da curva."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config" / "mouse-fine-tuning"

# Presets
BUILTIN_PRESETS_SRC = REPO_DIR / "data" / "builtin-presets"
PRESETS_DIR = CONFIG_DIR / "presets"
BUILTIN_RUNTIME_DIR = PRESETS_DIR / "_builtin"
CUSTOM_PRESETS_DIR = PRESETS_DIR / "custom"

# Devices
DEVICES_CONFIG_PATH = CONFIG_DIR / "devices.json"

# Legacy
LEGACY_CURVE_PATH = CONFIG_DIR / "curve.json"

# IPC
SOCKET_PATH = (
    Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "mouse-curve-daemon.sock"
)

DEFAULT_CURVE = {
    "sensitivity": 1.0,
    "gain": 0.1,
    "power": 1.5,
    "deadzone": 0.0,
    "max_multiplier": 3.0,
}

_NAME_RE = re.compile(r"[^A-Za-z0-9 _\-]")


# ---------- curva math ----------


def apply_curve(dx: float, dy: float, speed_pps: float, curve: dict) -> tuple[float, float]:
    """Aplica curva paramétrica a um par (dx, dy). speed_pps é a magnitude pixels/s."""
    effective = max(0.0, speed_pps - curve.get("deadzone", 0.0))
    accel = curve.get("gain", 0.0) * (effective / 1000.0) ** curve.get("power", 1.5)
    mult = min(
        curve.get("sensitivity", 1.0) * (1.0 + accel),
        curve.get("max_multiplier", 3.0),
    )
    return dx * mult, dy * mult


def multiplier_for(speed_pps: float, curve: dict) -> float:
    """Multiplicador puro pra speed_pps (usado em preview / live monitor)."""
    effective = max(0.0, speed_pps - curve.get("deadzone", 0.0))
    accel = curve.get("gain", 0.0) * (effective / 1000.0) ** curve.get("power", 1.5)
    return min(
        curve.get("sensitivity", 1.0) * (1.0 + accel),
        curve.get("max_multiplier", 3.0),
    )


# ---------- presets ----------


def sanitize_preset_name(name: str) -> str:
    """Sanitiza pra ser um basename de arquivo seguro."""
    cleaned = _NAME_RE.sub("", name).strip()
    return cleaned[:64] or "preset"


def _preset_slug(name: str) -> str:
    return sanitize_preset_name(name).lower().replace(" ", "-")


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BUILTIN_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOM_PRESETS_DIR.mkdir(parents=True, exist_ok=True)


def sync_builtin_presets() -> None:
    """Copia presets do data/builtin-presets/ pra ~/.config/.../_builtin/.
    Sobrescreve a cada start — atualizações do repo se propagam."""
    ensure_dirs()
    if not BUILTIN_PRESETS_SRC.exists():
        return
    # Limpar built-ins antigos
    for f in BUILTIN_RUNTIME_DIR.glob("*.json"):
        f.unlink()
    for src in BUILTIN_PRESETS_SRC.glob("*.json"):
        dst = BUILTIN_RUNTIME_DIR / src.name
        dst.write_text(src.read_text(), encoding="utf-8")


def _load_preset_file(path: Path, builtin: bool) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    name = data.get("name") or path.stem
    curve = {**DEFAULT_CURVE, **(data.get("curve") or {})}
    return {
        "name": name,
        "description": data.get("description", ""),
        "builtin": builtin,
        "order": data.get("order", 999),
        "curve": curve,
        "_path": path,
    }


def list_all_presets() -> list[dict]:
    """Retorna lista ordenada de todos os presets (built-ins primeiro)."""
    ensure_dirs()
    presets: list[dict] = []

    for path in BUILTIN_RUNTIME_DIR.glob("*.json"):
        p = _load_preset_file(path, builtin=True)
        if p:
            presets.append(p)

    for path in CUSTOM_PRESETS_DIR.glob("*.json"):
        p = _load_preset_file(path, builtin=False)
        if p:
            presets.append(p)

    presets.sort(key=lambda p: (not p["builtin"], p["order"], p["name"].lower()))
    return presets


def find_preset(name: str) -> dict | None:
    """Encontra preset pelo nome (case-insensitive)."""
    nm = name.lower()
    for p in list_all_presets():
        if p["name"].lower() == nm:
            return p
    return None


def save_custom_preset(name: str, description: str, curve: dict) -> dict:
    """Salva preset customizado. Retorna o dict salvo (com _path)."""
    ensure_dirs()
    safe = _preset_slug(name)
    path = CUSTOM_PRESETS_DIR / f"{safe}.json"
    data = {
        "name": sanitize_preset_name(name),
        "description": description,
        "curve": {k: float(curve.get(k, DEFAULT_CURVE[k])) for k in DEFAULT_CURVE},
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return {**data, "builtin": False, "order": 999, "_path": path}


def delete_custom_preset(name: str) -> bool:
    p = find_preset(name)
    if not p or p["builtin"]:
        return False
    path: Path = p["_path"]
    try:
        path.unlink()
        return True
    except OSError:
        return False


def rename_custom_preset(old_name: str, new_name: str) -> dict | None:
    p = find_preset(old_name)
    if not p or p["builtin"]:
        return None
    new_safe = _preset_slug(new_name)
    new_path = CUSTOM_PRESETS_DIR / f"{new_safe}.json"
    if new_path.exists():
        return None  # conflito
    old_path: Path = p["_path"]
    data = json.loads(old_path.read_text(encoding="utf-8"))
    data["name"] = sanitize_preset_name(new_name)
    new_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    old_path.unlink()
    return _load_preset_file(new_path, builtin=False)


# ---------- devices.json ----------


def device_id_from_evdev(dev) -> str:
    """Gera ID estável: 'vendor:product' em hex 4-digit lowercase."""
    info = dev.info
    return f"{info.vendor:04x}:{info.product:04x}"


def load_devices_config() -> dict:
    """Retorna {'devices': [...]}. Cria default se não existe."""
    ensure_dirs()
    if not DEVICES_CONFIG_PATH.exists():
        # Migrar do legado se existir
        migrated = _migrate_legacy_curve_config()
        if migrated:
            return migrated
        return {"devices": []}
    try:
        data = json.loads(DEVICES_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data.get("devices"), list):
            data["devices"] = []
        return data
    except (OSError, json.JSONDecodeError):
        return {"devices": []}


def save_devices_config(config: dict) -> None:
    ensure_dirs()
    tmp = DEVICES_CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DEVICES_CONFIG_PATH)


def upsert_device(config: dict, device_id: str, name: str, **fields) -> dict:
    """Insere ou atualiza um device na config. Retorna o device entry."""
    for d in config["devices"]:
        if d.get("id") == device_id:
            d["name"] = name
            for k, v in fields.items():
                d[k] = v
            return d
    new_entry = {
        "id": device_id,
        "name": name,
        "preset": fields.get("preset", "Linear"),
        "enabled": fields.get("enabled", False),
    }
    new_entry.update(fields)
    config["devices"].append(new_entry)
    return new_entry


def find_device(config: dict, device_id: str) -> dict | None:
    for d in config["devices"]:
        if d.get("id") == device_id:
            return d
    return None


def _migrate_legacy_curve_config() -> dict | None:
    """Migra ~/.config/.../curve.json (v0.2) para devices.json (v0.3+).
    Cria um preset custom 'Migrated' com os parâmetros e atribui ao primeiro device."""
    if not LEGACY_CURVE_PATH.exists():
        return None
    try:
        legacy = json.loads(LEGACY_CURVE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    curve = {**DEFAULT_CURVE, **(legacy.get("curve") or {})}
    save_custom_preset(
        "Migrado da v0.2",
        "Curva importada do curve.json da versão anterior.",
        curve,
    )
    devices_cfg = {"devices": []}
    # Não temos device_id no legacy — fica vazio. Daemon enumera depois.
    save_devices_config(devices_cfg)
    # Renomear legacy pra não migrar de novo
    backup = LEGACY_CURVE_PATH.with_suffix(".json.v0.2.bak")
    try:
        LEGACY_CURVE_PATH.rename(backup)
    except OSError:
        pass
    return devices_cfg
