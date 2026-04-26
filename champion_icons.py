# -*- coding: utf-8 -*-
"""英雄图标：从 Data Dragon 一次性拉取并缓存到本地 static/champions/。

- 图标来源：https://ddragon.leagueoflegends.com/cdn/{ver}/img/champion/{id}.png
- 元数据来源：https://ddragon.leagueoflegends.com/cdn/{ver}/data/zh_CN/champion.json
- 本地路径：<repo>/static/champions/{id}.png
- 对外接口：get_champion_list() -> [{id, name_cn, name_en, title_cn, icon}]
  icon 为相对 URL，如 "/static/champions/Yasuo.png"
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

import requests

log = logging.getLogger("ARAM")

_BASE_DIR = Path(__file__).parent
_ICON_DIR = _BASE_DIR / "static" / "champions"
_META_FILE = _BASE_DIR / "static" / "champions.json"

_DDRAGON_VER_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
_DDRAGON_BASE = "https://ddragon.leagueoflegends.com/cdn"
_HEADERS = {"User-Agent": "Mozilla/5.0"}

_cache_list: List[Dict[str, Any]] = []


def _ddragon_version() -> str:
    try:
        return requests.get(_DDRAGON_VER_URL, headers=_HEADERS, timeout=10).json()[0]
    except Exception as e:
        log.warning(f"[Icons] DDragon 版本获取失败，回退 15.20.1: {e}")
        return "15.20.1"


def _download_one(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 0:
        return True
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            log.warning(f"[Icons] HTTP {r.status_code} {url}")
            return False
        dest.write_bytes(r.content)
        return True
    except Exception as e:
        log.warning(f"[Icons] 下载失败 {url}: {e}")
        return False


def ensure_champion_icons(force: bool = False) -> List[Dict[str, Any]]:
    """确保所有英雄图标已下载，返回英雄列表。结果内存缓存。"""
    global _cache_list
    if _cache_list and not force:
        return _cache_list

    # 优先复用已生成的元数据文件（避免每次启动都打 DDragon）
    if _META_FILE.exists() and not force:
        try:
            data = json.loads(_META_FILE.read_text(encoding="utf-8"))
            missing = [c for c in data if not (_ICON_DIR / f"{c['id']}.png").exists()]
            if not missing:
                _cache_list = data
                log.info(f"[Icons] 使用已缓存元数据: {len(data)} 位英雄")
                return _cache_list
            log.info(f"[Icons] 检测到 {len(missing)} 个图标缺失，将重新下载")
        except Exception as e:
            log.warning(f"[Icons] 读取 {_META_FILE} 失败，重建: {e}")

    _ICON_DIR.mkdir(parents=True, exist_ok=True)
    ver = _ddragon_version()
    meta_url = f"{_DDRAGON_BASE}/{ver}/data/zh_CN/champion.json"
    try:
        meta = requests.get(meta_url, headers=_HEADERS, timeout=15).json().get("data", {})
    except Exception as e:
        log.error(f"[Icons] 加载 {meta_url} 失败: {e}")
        return []

    champs: List[Dict[str, Any]] = []
    for cid, info in meta.items():
        # DDragon zh_CN: name="万花通灵"(称号), title="妮蔻"(英雄名)。用户搜的是英雄名。
        hero_name = info.get("title") or info.get("name") or cid
        epithet = info.get("name", "") if info.get("title") else ""
        champs.append({
            "id": cid,
            "name_cn": hero_name,
            "title_cn": epithet,
            "name_en": info.get("id", cid),
            "icon": f"/static/champions/{cid}.png",
        })
    champs.sort(key=lambda c: c["name_cn"])

    # 并发下载缺失的图标
    tasks = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for c in champs:
            url = f"{_DDRAGON_BASE}/{ver}/img/champion/{c['id']}.png"
            dest = _ICON_DIR / f"{c['id']}.png"
            tasks.append(pool.submit(_download_one, url, dest))
        done = sum(1 for f in as_completed(tasks) if f.result())
    log.info(f"[Icons] 下载完成 {done}/{len(champs)}（版本 {ver}）")

    try:
        _META_FILE.parent.mkdir(parents=True, exist_ok=True)
        _META_FILE.write_text(
            json.dumps(champs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning(f"[Icons] 写入 {_META_FILE} 失败: {e}")

    _cache_list = champs
    return _cache_list


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    out = ensure_champion_icons(force=True)
    print(f"Total: {len(out)}; sample:", out[:3])
