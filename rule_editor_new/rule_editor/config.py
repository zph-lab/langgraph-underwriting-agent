"""配置加载。

约定：项目根 = 本文件上一级（rule_editor/ 的父目录），即 Intelligent_Rule_Editor/。
数据目录（车险/）位于项目根下。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# rule_editor/config.py -> 项目根（Intelligent_Rule_Editor/）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_config_cache: dict[str, Any] | None = None


def project_root() -> Path:
    return _PROJECT_ROOT


def load_config() -> dict[str, Any]:
    global _config_cache
    if _config_cache is None:
        cfg_path = Path(__file__).resolve().parent / "config.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        # 相对路径统一锚定到项目根
        _anchor_paths(cfg, _PROJECT_ROOT)
        _config_cache = cfg
    return _config_cache


def _anchor_paths(cfg: dict, root: Path) -> None:
    """把配置里的相对路径统一转成绝对路径。"""
    # 键尾为 _path / _dir / _db 的相对路径统一锚定到项目根
    PATH_SUFFIXES = ("_path", "_dir", "_db")
    for k, v in cfg.items():
        _fix_key(cfg, k, v, root)


def _fix_key(node, k, v, root):
    """递归修正以路径后缀结尾的相对路径键。"""
    if k.endswith(("_path", "_dir", "_db")):
        if isinstance(v, str) and not os.path.isabs(v):
            node[k] = str(root / v)
        return
    if isinstance(v, dict):
        for kk, vv in v.items():
            _fix_key(v, kk, vv, root)
    elif isinstance(v, list):
        for it in v:
            if isinstance(it, dict):
                for kk, vv in it.items():
                    _fix_key(it, kk, vv, root)


def get(section: str, key: str, default: Any = None) -> Any:
    cfg = load_config()
    return cfg.get(section, {}).get(key, default)


def data_path(key: str) -> str:
    return get("data", key)
