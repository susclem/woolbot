"""配置加载。

一份 config.json 驱动全部行为；AI 模型完全可自定义（base_url / model /
api_key / vision_model），并且支持用环境变量覆盖敏感项，方便 Docker 部署。
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

# 日历页面监听的端口 —— **全项目只有这一个默认值**（`web.py::resolve_port`、
# `settings.front_url()`、`main.py` 的兜底、config.json 的默认都引用它）。
# 改它必须同时改另外三处，否则「页面打不开」或「健康检查永远 unhealthy」：
#   1. `docker-compose.yml` 的端口映射 —— 左边（宿主机）和右边（容器内）都跟着改；
#   2. `Dockerfile` 的 `EXPOSE` 与 `HEALTHCHECK` 里的地址；
#   3. NAS 上那层反代（2026-10-10 实测是 Lucky 的 5668 那条规则）指着这个端口，
#      不改就变成 502 Bad Gateway。
DEFAULT_WEB_PORT = 16886

DEFAULTS: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "db_path": "data/woolbot.db",
    "provider": "deepseek",
    "providers": {
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "api_key": "",
            "model": "deepseek-v4-flash",
            "vision_model": "",
            "temperature": 0,
            "timeout": 120,
        }
    },
    "query": {
        # strict = 只匹配「开抢时刻正好是这一分钟」
        "mode": "strict",
        # 附加分区（不影响严格命中集合，只是标注清楚的额外信息）
        "show_ongoing": True,
    },
    "web": {
        "enabled": True,
        "host": "0.0.0.0",
        "port": DEFAULT_WEB_PORT,
        "token": "",
    },
    "wechat": {
        "enabled": True,
        "state_file": "data/weixin_state.json",
        "media_dir": "data/media",
        "allowed_users": [],
        "session_hours": 24,
        "warn_before_hours": 2,
    },
    "log": {"level": "INFO"},
}

# 环境变量覆盖：WOOLBOT_<UPPER_SNAKE> 映射到配置的 dotted path
_ENV_MAP = {
    "WOOLBOT_PROVIDER": ("provider",),
    "WOOLBOT_API_KEY": ("providers", "$current", "api_key"),
    "WOOLBOT_BASE_URL": ("providers", "$current", "base_url"),
    "WOOLBOT_MODEL": ("providers", "$current", "model"),
    "WOOLBOT_VISION_MODEL": ("providers", "$current", "vision_model"),
    "WOOLBOT_TIMEZONE": ("timezone",),
    "WOOLBOT_DB_PATH": ("db_path",),
    "WOOLBOT_PORT": ("web", "port"),
    "WOOLBOT_WEB_TOKEN": ("web", "token"),
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env(cfg: dict) -> None:
    """把环境变量写进配置。$current 表示当前选中的 provider。"""
    for env_name, path in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        parts = list(path)
        if "$current" in parts:
            idx = parts.index("$current")
            parts[idx] = cfg.get("provider") or "deepseek"
        node = cfg
        for key in parts[:-1]:
            node = node.setdefault(key, {})
        value: Any = raw
        if parts[-1] == "port":
            try:
                value = int(raw)
            except ValueError:
                continue
        node[parts[-1]] = value


class Config:
    """轻量配置对象，支持 cfg.web.port 与 cfg["web"]["port"] 两种读法。"""

    def __init__(self, data: dict[str, Any], root: Path = PROJECT_ROOT):
        self._data = data
        self.root = root

    def __getattr__(self, item: str) -> Any:
        try:
            value = self._data[item]
        except KeyError as exc:  # pragma: no cover - 配置项写错时才会走到
            raise AttributeError(item) from exc
        if isinstance(value, dict):
            return Config(value, self.root)
        return value

    def __getitem__(self, item: str) -> Any:
        return self.__getattr__(item)

    def get(self, item: str, default: Any = None) -> Any:
        value = self._data.get(item, default)
        if isinstance(value, dict):
            return Config(value, self.root)
        return value

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def path(self, relative: str) -> Path:
        """把一个相对路径解析成项目根目录下的绝对路径。"""
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)

    # -- 当前 provider ------------------------------------------------

    @property
    def active_provider_name(self) -> str:
        """当前选中的 provider 名字。名字不存在时退回第一个可用的。"""
        name = self._data.get("provider") or "deepseek"
        providers = self._data.get("providers") or {}
        if name in providers or not providers:
            return name
        return next(iter(providers))

    @property
    def active_provider(self) -> dict[str, Any]:
        providers = self._data.get("providers") or {}
        if not providers:
            raise RuntimeError("config.json 里没有任何 provider，请先配置一个 AI 模型")
        return dict(providers[self.active_provider_name])


def load(path: Path | None = None) -> Config:
    cfg_path = Path(path) if path else CONFIG_PATH
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"config.json 解析失败: {exc}") from exc
    cfg = _deep_merge(DEFAULTS, raw)
    _apply_env(cfg)
    return Config(cfg)


def ensure_config_file(path: Path | None = None) -> Path:
    """首次运行时把示例配置落到 config.json。"""
    cfg_path = Path(path) if path else CONFIG_PATH
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps(DEFAULTS, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return cfg_path
