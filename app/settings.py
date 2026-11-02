"""Hot-editable, persistently stored settings (secrets never returned to browser)."""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

RESOLUTIONS = ("2160p", "1080p", "720p", "576p", "480p", "360p")
NOTIFY = ("found", "queued", "completed", "failed")
SECRET_FIELDS = ("hydra_key", "sab_key", "pushover_key", "pushover_user")

DEFAULTS = {
    "hydra_url": "", "hydra_key": "", "sab_url": "", "sab_key": "",
    "pushover_key": "", "pushover_user": "",
    "notification_types": ["found", "completed", "failed"],
    "scan_interval_minutes": 30,
    "max_queues_per_scan": 3,
    "clean_search_terms": True,
    "strip_release_groups": True,
    "resolution_enabled": False,  # opt-in prevents breaking existing non-video watches
    "resolution_order": ["1080p", "2160p", "720p", "480p"],
    "fallback_min_resolution": "720p",
    "allow_unknown_resolution": False,
    "dedupe_across_resolutions": True,
    "watchlist_columns": 2,  # 1 = full-width rows, 2 = two-column cards
    "sab_missing_policy": "keep_queued",  # opt-in: assume_completed
    "sab_missing_grace_minutes": 60,  # first absence must persist this long
    "history_retention_days": 14,  # terminal match rows and event log; 0 disables
    "completed_watch_retention_days": 7,  # after confirmed/assumed success; 0 disables
    "pushover_privacy": "full",  # full, watch_only, generic
    "theme_mode": "dark",  # dark or light
    "theme_color": "#56d7ba",  # accent hex color

}


def validate_url(text: str) -> str:
    url = text.strip().rstrip("/")
    if not url:
        return ""
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or
        parsed.username is not None or parsed.password is not None or
        parsed.query or parsed.fragment or any(ch.isspace() for ch in url)):
        raise ValueError("Service URL must be HTTP(S) without credentials, query string or fragment")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid service URL port") from exc
    return url


def verify(data: dict) -> dict:
    out = {**DEFAULTS}
    out.update({k: v for k, v in data.items() if k in DEFAULTS})
    for key in ("hydra_url", "sab_url"):
        if not isinstance(out[key], str):
            raise ValueError(f"{key} must be text")
        out[key] = validate_url(out[key])
    for key in SECRET_FIELDS:
        if not isinstance(out[key], str) or len(out[key]) > 512:
            raise ValueError(f"Invalid {key}")
    for key in ("scan_interval_minutes", "max_queues_per_scan", "sab_missing_grace_minutes"):
        if type(out[key]) is not int:
            raise ValueError(f"{key} must be a whole number")
    if not 5 <= out["scan_interval_minutes"] <= 10080:
        raise ValueError("Scan interval must be 5–10080 minutes")
    if not 1 <= out["max_queues_per_scan"] <= 20:
        raise ValueError("Max queues per scan must be 1–20")
    if not 5 <= out["sab_missing_grace_minutes"] <= 10080:
        raise ValueError("Missing SAB job grace period must be 5–10080 minutes")
    if out["sab_missing_policy"] not in ("keep_queued", "assume_completed"):
        raise ValueError("Invalid missing SAB job policy")
    if type(out["watchlist_columns"]) is not int or out["watchlist_columns"] not in (1, 2):
        raise ValueError("Watchlist columns must be 1 or 2")
    for key in ("history_retention_days", "completed_watch_retention_days"):
        if type(out[key]) is not int or not 0 <= out[key] <= 3650:
            raise ValueError(f"{key} must be between 0 and 3650 days")
    if out["pushover_privacy"] not in ("full", "watch_only", "generic"):
        raise ValueError("Invalid Pushover privacy level")
    if out["theme_mode"] not in ("dark", "light"):
        raise ValueError("Theme must be dark or light")
    if not isinstance(out["theme_color"], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", out["theme_color"]):
        raise ValueError("Theme color must be a six-digit hex color")
    out["theme_color"] = out["theme_color"].lower()
    for key in ("clean_search_terms", "strip_release_groups", "resolution_enabled", "allow_unknown_resolution", "dedupe_across_resolutions"):
        if type(out[key]) is not bool:
            raise ValueError(f"{key} must be true or false")
    order = out["resolution_order"]
    if not isinstance(order, list) or not order or len(order) != len(set(order)) or any(x not in RESOLUTIONS for x in order):
        raise ValueError("Resolution order must contain unique known resolutions")
    if out["fallback_min_resolution"] not in RESOLUTIONS:
        raise ValueError("Invalid minimum fallback resolution")
    notify = out["notification_types"]
    if not isinstance(notify, list) or len(notify) != len(set(notify)) or any(x not in NOTIFY for x in notify):
        raise ValueError("Invalid notification event types")
    return out


def initial_env() -> dict:
    data = dict(DEFAULTS)
    for key, env in {
        "hydra_url": "HYDRA_URL", "hydra_key": "HYDRA_API_KEY", "sab_url": "SAB_URL",
        "sab_key": "SAB_API_KEY", "pushover_key": "PUSHOVER_APP_TOKEN",
        "pushover_user": "PUSHOVER_USER_KEY",
    }.items():
        data[key] = os.getenv(env, data[key])
    data["notification_types"] = [n.strip() for n in os.getenv("NOTIFY_EVENTS", "found,completed,failed").split(",") if n.strip()]
    try:
        data["max_queues_per_scan"] = int(os.getenv("MAX_QUEUES_PER_SCAN", "3"))
    except ValueError:
        data["max_queues_per_scan"] = 3
    data["max_queues_per_scan"] = min(20, max(1, data["max_queues_per_scan"]))
    return data


class SettingsFile:
    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Configuration must not be a symlink")
        if self.path.exists():
            settings = verify(json.loads(self.path.read_text(encoding="utf-8")))
            # Fix overly permissive config on older installs, if possible.
            os.chmod(self.path, 0o600)
        else:
            settings = verify(initial_env())
            self._atomic_write(settings)
        self.data = settings  # The engine holds this same dict for immediate changes.

    def _atomic_write(self, data: dict):
        temporary = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"schema_version": 1, **data}, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def public(self):
        with self.lock:
            return {**{k: v for k, v in self.data.items() if k not in SECRET_FIELDS},
                    "configured_secrets": {key: bool(self.data[key]) for key in SECRET_FIELDS}}

    def update(self, submitted: dict):
        with self.lock:
            next_data = dict(self.data)
            for key, val in submitted.items():
                if key not in DEFAULTS:
                    raise ValueError(f"Unknown setting: {key}")
                if key in SECRET_FIELDS and val is None:
                    continue  # Blank fields don't overwrite stored keys.
                next_data[key] = val
            next_data = verify(next_data)
            self._atomic_write(next_data)  # Never mutate runtime if saving fails.
            self.data.clear()
            self.data.update(next_data)
            return self.public()
