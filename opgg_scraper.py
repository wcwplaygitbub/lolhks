# -*- coding: utf-8 -*-
"""ARAM 助手 - op.gg 海克斯大乱斗（ARAM Mayhem）数据抓取

数据源：op.gg 的无 WAF JSON API 子域 `https://lol-api-champion.op.gg`
（由 LeeSin 项目 electron/core/data/opgg-client.ts 验证可直接无鉴权访问）

ARAM Mayhem 模式在 op.gg 没有独立端点，按 LeeSin 的做法合并两个源：
  - 出装：/api/{region}/champions/aram/{id}/none?tier=emerald_plus        （普通 ARAM 的装备）
  - 海克斯（augments）：/api/{region}/champions/arena/{id}?tier=all        （斗魂竞技场的增幅池，和 Mayhem 共用）

增幅元数据（id→中文名/图标/稀有度）来源：
  https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/zh_cn/v1/cherry-augments.json

装备 / 召唤师技能元数据：Data Dragon。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests

from apexlol_data import resolve_champion_id

log = logging.getLogger("ARAM")

_API_BASE = "https://lol-api-champion.op.gg"
_CDRAGON_AUGMENTS = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/zh_cn/v1/cherry-augments.json"
)
_CDRAGON_ASSET_BASE = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default"
)
_DDRAGON_VER_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
_DDRAGON_BASE = "https://ddragon.leagueoflegends.com/cdn"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# ==================== 资源缓存 ====================

_ddragon_cache: Dict[str, Any] = {}
_augments_cache: Dict[int, Dict[str, str]] = {}
_champion_key_map: Dict[str, int] = {}  # DDragon ID (Corki) -> numeric key (42)

# 请求结果缓存（2 小时 TTL）
_CACHE_TTL_SEC = 2 * 60 * 60
_result_cache: Dict[str, tuple] = {}  # champion_id -> (expire_ts, result_dict)


def _ddragon_version() -> str:
    if _ddragon_cache.get("version"):
        return _ddragon_cache["version"]
    try:
        v = requests.get(_DDRAGON_VER_URL, headers=_HEADERS, timeout=10).json()[0]
    except Exception as e:
        log.warning(f"[OPGG] 获取 DDragon 版本失败，回退 15.20.1: {e}")
        v = "15.20.1"
    _ddragon_cache["version"] = v
    return v


def _ddragon_data(kind: str, lang: str = "zh_CN") -> Dict[str, Any]:
    key = f"{kind}_{lang}"
    if key in _ddragon_cache:
        return _ddragon_cache[key]
    v = _ddragon_version()
    url = f"{_DDRAGON_BASE}/{v}/data/{lang}/{kind}.json"
    try:
        _ddragon_cache[key] = requests.get(url, headers=_HEADERS, timeout=15).json().get("data", {})
    except Exception as e:
        log.warning(f"[OPGG] 加载 {url} 失败: {e}")
        _ddragon_cache[key] = {}
    return _ddragon_cache[key]


def _load_champion_keys() -> Dict[str, int]:
    if _champion_key_map:
        return _champion_key_map
    for cid, info in _ddragon_data("champion").items():
        try:
            _champion_key_map[cid] = int(info.get("key"))
        except Exception:
            pass
    log.info(f"[OPGG] DDragon champion 映射加载: {len(_champion_key_map)} 条")
    return _champion_key_map


def _load_augments() -> Dict[int, Dict[str, str]]:
    if _augments_cache:
        return _augments_cache
    try:
        data = requests.get(_CDRAGON_AUGMENTS, headers=_HEADERS, timeout=15).json()
    except Exception as e:
        log.warning(f"[OPGG] 加载 cherry-augments 失败: {e}")
        return _augments_cache
    for aug in data:
        icon_path = (aug.get("augmentSmallIconPath") or "").replace("/lol-game-data/assets/", "").lower()
        _augments_cache[int(aug["id"])] = {
            "name": aug.get("nameTRA", ""),
            "icon_url": f"{_CDRAGON_ASSET_BASE}/{icon_path}" if icon_path else "",
            "rarity": aug.get("rarity", ""),
        }
    log.info(f"[OPGG] cherry-augments 加载: {len(_augments_cache)} 条")
    return _augments_cache


def _augment_info(augment_id: int) -> Dict[str, Any]:
    augs = _load_augments()
    info = augs.get(augment_id, {})
    return {
        "id": augment_id,
        "name": info.get("name", f"增幅 #{augment_id}"),
        "icon_url": info.get("icon_url", ""),
        "rarity": info.get("rarity", ""),
    }


# ==================== op.gg API 调用 ====================

def _get_json(url: str, retries: int = 3) -> Optional[Dict[str, Any]]:
    last_err = None
    for attempt in range(retries):
        try:
            # 每次都用新 Session，避免连接池里复用了半关闭的 TCP
            with requests.Session() as s:
                r = s.get(url, headers=_HEADERS, timeout=15)
            if r.status_code != 200:
                log.warning(f"[OPGG] HTTP {r.status_code}: {url}")
                return None
            return r.json()
        except Exception as e:
            last_err = e
            log.warning(f"[OPGG] 请求失败 (第 {attempt+1}/{retries} 次) {url}: {e}")
            if attempt < retries - 1:
                time.sleep(0.8 * (attempt + 1))
    log.warning(f"[OPGG] 重试 {retries} 次后放弃: {url} | {last_err}")
    return None


def _fetch_arena_augments(champion_key: int, region: str = "kr") -> Optional[Dict[str, Any]]:
    url = f"{_API_BASE}/api/{region}/champions/arena/{champion_key}?tier=all"
    return _get_json(url)


# ==================== 解析 ====================

def _augments(augment_group: List[Dict[str, Any]], top: int = 18) -> List[Dict[str, Any]]:
    """arena augment_group: [{rarity, augments:[{id,win,play,total_place,first_place,pick_rate}]}, ...]
    扁平化 → 按 pick_rate 降序，返回 top 条。
    """
    flat: List[Dict[str, Any]] = []
    for group in augment_group or []:
        for aug in group.get("augments", []):
            info = _augment_info(aug["id"])
            play = aug.get("play", 0)
            win = aug.get("win", 0)
            tp = aug.get("total_place", 0)
            flat.append({
                **info,
                "play": play,
                "win": win,
                "pick_rate": round(aug.get("pick_rate", 0) * 100, 2),
                "win_rate": round(win * 100 / play, 2) if play else None,
                "avg_place": round(tp / play, 2) if play else None,
            })
    flat.sort(key=lambda a: a.get("pick_rate", 0), reverse=True)
    return flat[:top]


# ==================== 对外主函数 ====================

def fetch_aram_mayhem(champion_name: str) -> Dict[str, Any]:
    """抓取 op.gg 的海克斯大乱斗海克斯强化（augments）数据。

    仅请求 Arena 端点（和 Mayhem 共用增幅池），出装/召唤师技能等不再拉取。
    结果带 2 小时本地缓存。

    返回 {
        "ok": bool,
        "champion_id": str, "champion_key": int,
        "version": str,
        "augments": [...],      # 扁平化按选取率排序
        "cached": bool,         # 本次是否命中缓存
    }
    """
    champion_id = resolve_champion_id(champion_name)
    if not champion_id:
        return {"ok": False, "error": f"无法识别英雄：{champion_name}"}

    # 查缓存
    now = time.time()
    cached = _result_cache.get(champion_id)
    if cached and cached[0] > now:
        result = dict(cached[1])
        result["cached"] = True
        return result

    key_map = _load_champion_keys()
    champion_key = key_map.get(champion_id)
    if champion_key is None:
        return {"ok": False, "error": f"英雄 {champion_id} 无法映射到数字 ID"}

    arena = _fetch_arena_augments(champion_key)
    if not arena:
        return {
            "ok": False,
            "champion_id": champion_id,
            "champion_key": champion_key,
            "error": "海克斯强化数据暂不可用",
        }

    d = arena.get("data", {}) or {}
    result = {
        "ok": True,
        "champion_id": champion_id,
        "champion_key": champion_key,
        "version": arena.get("meta", {}).get("version", ""),
        "augments": _augments(d.get("augment_group", []), top=18),
        "cached": False,
    }

    # 写缓存
    _result_cache[champion_id] = (now + _CACHE_TTL_SEC, result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    import sys
    from apexlol_data import load_cache
    import config
    load_cache(config.APEXLOL_CACHE_DIR)

    name = sys.argv[1] if len(sys.argv) > 1 else "亚索"
    out = fetch_aram_mayhem(name)
    print(json.dumps(out, indent=2, ensure_ascii=False)[:3000])
