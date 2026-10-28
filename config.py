from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://mineru.net/api/v4"
TOKEN_KEYS = ("MINERU_API_TOKEN", "MINERU_API_KEY", "MINERU_TOKEN")
BASE_KEYS = ("MINERU_API_BASE_URL", "MINERU_BASE_URL")
ENV_KEYS = TOKEN_KEYS + BASE_KEYS + ("MINERU_LOCAL_CONFIG_FILE", "MINERU_LOCAL_ENV_FILE")


@dataclass(frozen=True)
class ApiSettings:
    token: str
    base_url: str


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return (base / "mineru-local" / "config.json").resolve()


def _strip_inline_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char in {"'", '"'}:
            quote = "" if quote == char else char if not quote else quote
        elif char == "#" and not quote:
            return value[:index]
    return value


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if separator and key in TOKEN_KEYS + BASE_KEYS:
            values[key] = _strip_quotes(_strip_inline_comment(value).strip())
    return values


def _read_json_config(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("MinerU配置文件必须是JSON对象。")
    token = raw.get("api_token", raw.get("MINERU_API_TOKEN", ""))
    if not isinstance(token, str):
        raise ValueError("MinerU配置文件中的api_token必须是字符串。")
    return {"MINERU_API_TOKEN": token}


def _read_config_file(path: Path) -> dict[str, str]:
    if path.suffix.lower() == ".env" or path.name.endswith(".env"):
        return _read_env_file(path)
    return _read_json_config(path)


def load_settings(config_file: Path | None = None) -> ApiSettings:
    configured_path = os.environ.get("MINERU_LOCAL_CONFIG_FILE", "").strip()
    legacy_path = os.environ.get("MINERU_LOCAL_ENV_FILE", "").strip()
    selected_path = config_file
    if selected_path is None and configured_path:
        selected_path = Path(configured_path).expanduser().resolve()
    if selected_path is None and legacy_path:
        selected_path = Path(legacy_path).expanduser().resolve()
    if selected_path is None:
        selected_path = default_config_path()
    file_values = _read_config_file(selected_path) if selected_path.is_file() else {}
    token = next(
        (os.environ[key].strip() for key in TOKEN_KEYS if os.environ.get(key, "").strip()),
        next((file_values[key].strip() for key in TOKEN_KEYS if file_values.get(key, "").strip()), ""),
    )
    base_url = next(
        (os.environ[key].strip() for key in BASE_KEYS if os.environ.get(key, "").strip()),
        next((file_values[key].strip() for key in BASE_KEYS if file_values.get(key, "").strip()), DEFAULT_BASE_URL),
    ).rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("MinerU API地址必须为不含凭据和查询参数的HTTPS地址。")
    return ApiSettings(token=token, base_url=base_url)
