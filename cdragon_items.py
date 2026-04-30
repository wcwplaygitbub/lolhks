# -*- coding: utf-8 -*-
"""ARAM 助手 - 装备描述数据

数据源：CommunityDragon items.json（zh_cn）
  - 包含装备 ID / 中文名 / 描述 / 图标 / 价格 / 合成路线等

缓存策略（与 cdragon_augments.py 一致）：
  - 每个装备独立 cached_at，2 小时 TTL
  - 启动时预热，含重试 + 过时缓存兜底
  - 后台定期检查过期并按需刷新
  - API 调用只读缓存
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("ARAM")

_CDRAGON_ITEMS_ZH_CN = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/zh_cn/v1/items.json"
)
_CDRAGON_ITEMS_DEFAULT = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/default/v1/items.json"
)
_CDRAGON_ASSET_BASE = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default"
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain",
}

# ---- 缓存参数 ----
_ITEM_CACHE_TTL = 2 * 60 * 60   # 单个装备缓存 TTL：2 小时
_CHECK_INTERVAL = 30 * 60       # 定期检查间隔：30 分钟
_MAX_RETRIES = 2                # 启动预热最大重试次数

# ---- 全局状态 ----
_item_cache: Optional[Dict[int, Dict[str, Any]]] = None  # {id: {name, description, icon_url, price, ...}}
_item_name_index: Optional[Dict[str, int]] = None  # {中文名: 优先ID} 用于按名查找
_lock = threading.Lock()
_building = False
_checker_started = False


# ==================== 描述清洗 ====================

def _clean_item_description(desc: str) -> str:
    """将 CDragon 装备描述 HTML 转为可读纯文本。"""
    if not desc:
        return ""
    # <attention>val</attention> → val
    text = re.sub(r'<attention>([^<]*)</attention>', r'\1', desc)
    # <passive>name</passive> → name:
    text = re.sub(r'<passive>([^<]*)</passive>', r'\1: ', text)
    # <scaleStat>val</scaleStat> → val
    text = re.sub(r'<scaleStat>([^<]*)</scaleStat>', r'\1', text)
    # <scaleAP>val</scaleAP> → val
    text = re.sub(r'<scaleAP>([^<]*)</scaleAP>', r'\1', text)
    # <status>val</status> → val
    text = re.sub(r'<status>([^<]*)</status>', r'\1', text)
    # <flavorText>val</flavorText> → val
    text = re.sub(r'<flavorText>([^<]*)</flavorText>', r'\1', text)
    # <stats>...</stats> → 内容
    text = re.sub(r'<stats>([^<]*)</stats>', r'\1', text)
    # <mainText>...</mainText> → 内容
    text = re.sub(r'<mainText>([^<]*)</mainText>', r'\1', text)
    # <br> / <br /> → newline
    text = re.sub(r'<br\s*/?>', '\n', text)
    # Remove remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Clean up whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    return text.strip()


# ==================== CDragon 数据获取 ====================

def _fetch_cdragon_items() -> Dict[int, Dict[str, Any]]:
    """从 CDragon zh_cn 获取装备数据。返回 {id: {name, description, icon_url, price, ...}}。"""
    try:
        data = requests.get(_CDRAGON_ITEMS_ZH_CN, headers=_HEADERS, timeout=20).json()
        result = {}
        for item in data:
            item_id = item.get('id')
            if not item_id:
                continue
            name = item.get('name', '')
            if not name:
                continue

            icon_path = (item.get('iconPath') or '').replace(
                '/lol-game-data/assets/', ''
            ).lower()
            icon_url = f"{_CDRAGON_ASSET_BASE}/{icon_path}" if icon_path else ''

            raw_desc = item.get('description', '')
            clean_desc = _clean_item_description(raw_desc)

            result[item_id] = {
                'name': name,
                'description': clean_desc,
                'icon_url': icon_url,
                'price': item.get('priceTotal', 0),
                'from': item.get('from', []),
                'to': item.get('to', []),
                'in_store': item.get('inStore', False),
                'categories': item.get('categories', []),
            }

        log.info(f"[ItemDesc] CDragon zh_cn 装备数据: {len(result)} 个")
        return result
    except Exception as e:
        log.warning(f"[ItemDesc] CDragon 装备数据获取失败: {e}")
        return {}


# ==================== 名称索引 ====================

def _build_name_index(items: Dict[int, Dict[str, Any]]) -> Dict[str, int]:
    """构建中文名 → 优先ID 的索引。

    同名装备（如无尽之刃 ID=3031 和 223031）优先选择非 ARAM 版本（ID < 220000）。
    """
    index: Dict[str, int] = {}
    # 先按 ID 排序，确保非 ARAM 版本先处理
    for item_id in sorted(items.keys()):
        info = items[item_id]
        name = info.get('name', '')
        if not name:
            continue
        # 优先非 ARAM 版本（ID < 220000）
        existing = index.get(name)
        if existing is None or item_id < 220000:
            index[name] = item_id
    return index


# ==================== 磁盘缓存 ====================

_CACHE_FILENAME = "item_descriptions.json"


def _cache_path(cache_dir: str) -> Path:
    return Path(cache_dir) / _CACHE_FILENAME


def _load_from_cache(cache_dir: str, allow_expired: bool = False) -> Optional[Dict]:
    """从本地 JSON 缓存加载装备数据。"""
    p = _cache_path(cache_dir)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding='utf-8'))
        raw = obj.get('items', {})
        if not raw:
            return None

        # JSON 键是字符串，转回 int
        data = {}
        global_updated = obj.get('updated_at', 0)
        for item_id_str, info in raw.items():
            try:
                item_id = int(item_id_str)
            except (ValueError, TypeError):
                continue
            if 'cached_at' not in info:
                info['cached_at'] = global_updated
            data[item_id] = info

        if not allow_expired:
            now = time.time()
            max_cached = max((info.get('cached_at', 0) for info in data.values()), default=0)
            if now - max_cached > _ITEM_CACHE_TTL:
                log.info("[ItemDesc] 磁盘缓存已过期")
                return None

        log.info(f"[ItemDesc] 磁盘缓存加载: {len(data)} 条{'（允许过期）' if allow_expired else ''}")
        return data
    except Exception as e:
        log.warning(f"[ItemDesc] 缓存读取失败: {e}")
        return None


def _save_to_cache(cache_dir: str, data: Dict) -> None:
    """保存到本地 JSON 缓存。"""
    p = _cache_path(cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {'items': data}
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    log.info(f"[ItemDesc] 缓存已写入: {p} ({len(data)} 条)")


# ==================== 后台构建（含重试） ====================

def _count_expired() -> int:
    """统计当前缓存中过期的装备数量。"""
    if _item_cache is None:
        return 0
    now = time.time()
    return sum(1 for info in _item_cache.values()
               if now - info.get('cached_at', 0) > _ITEM_CACHE_TTL)


def _build_in_background_with_retry(cache_dir: str) -> None:
    """后台全量构建，带重试。"""
    global _item_cache, _item_name_index, _building

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            log.info(f"[ItemDesc] 全量构建 (尝试 {attempt}/{_MAX_RETRIES})...")
            data = _fetch_cdragon_items()
            if data:
                now = time.time()
                for item_id, info in data.items():
                    info['cached_at'] = now
                _item_cache = data
                _item_name_index = _build_name_index(data)
                if cache_dir:
                    _save_to_cache(cache_dir, data)
                with_desc = sum(1 for v in data.values() if v.get('description'))
                log.info(f"[ItemDesc] 全量构建成功: {len(data)} 个装备, {with_desc} 个有描述")
                _building = False
                return
            log.warning(f"[ItemDesc] 全量构建返回空数据 (尝试 {attempt}/{_MAX_RETRIES})")
        except Exception as e:
            log.error(f"[ItemDesc] 全量构建异常 (尝试 {attempt}/{_MAX_RETRIES}): {e}")

        if attempt < _MAX_RETRIES:
            wait = 10 * attempt
            log.info(f"[ItemDesc] 等待 {wait}s 后重试...")
            time.sleep(wait)

    _building = False
    if _item_cache:
        log.error(
            f"[ItemDesc] ⚠️ 所有重试均失败，继续使用过时缓存数据"
            f"（{_count_expired()} 个已过期，{len(_item_cache)} 个总计）"
        )
    else:
        log.error("[ItemDesc] ⚠️ 所有重试均失败，无任何缓存数据可用")


# ==================== 定期检查 ====================

def _periodic_checker(cache_dir: str) -> None:
    """后台线程：定期检查过期装备并按需刷新。"""
    while True:
        time.sleep(_CHECK_INTERVAL)
        try:
            _check_and_refresh(cache_dir)
        except Exception as e:
            log.error(f"[ItemDesc] 定期检查异常: {e}")


def _check_and_refresh(cache_dir: str) -> None:
    """检查过期装备，过期比例高则全量刷新。"""
    global _building

    if _item_cache is None or _building:
        return

    now = time.time()
    expired = sum(1 for info in _item_cache.values()
                  if now - info.get('cached_at', 0) > _ITEM_CACHE_TTL)

    if not expired:
        return

    total = len(_item_cache)
    ratio = expired / total if total else 0

    log.info(f"[ItemDesc] 定期检查: {expired}/{total} 个过期")

    if ratio > 0.3:
        log.info("[ItemDesc] 过期比例较高，执行全量刷新")
        with _lock:
            if _building:
                return
            _building = True
        t = threading.Thread(
            target=_build_in_background_with_retry, args=(cache_dir,), daemon=True
        )
        t.start()


def _start_checker(cache_dir: str) -> None:
    """启动定期检查线程（仅一次）。"""
    global _checker_started
    if _checker_started:
        return
    _checker_started = True
    t = threading.Thread(target=_periodic_checker, args=(cache_dir,), daemon=True)
    t.start()
    log.info(f"[ItemDesc] 定期检查线程已启动（间隔 {_CHECK_INTERVAL // 60} 分钟）")


# ==================== 公共接口 ====================

def get_item_descriptions(cache_dir: str = "") -> Dict[int, Dict[str, Any]]:
    """获取装备描述数据（只读缓存）。

    返回 {id: {name, description, icon_url, price, from, to, in_store, categories}}
    """
    global _item_cache

    if _item_cache is not None:
        return _item_cache

    # 磁盘缓存兜底
    if cache_dir:
        disk_data = _load_from_cache(cache_dir, allow_expired=True)
        if disk_data is not None:
            _item_cache = disk_data
            _item_name_index = _build_name_index(disk_data)
            return _item_cache

    return {}


def get_item_by_name(name: str) -> Optional[Dict[str, Any]]:
    """按中文名查找装备信息。同名装备优先返回非 ARAM 版本。"""
    if _item_name_index is None or _item_cache is None:
        return None
    item_id = _item_name_index.get(name)
    if item_id is None:
        return None
    return _item_cache.get(item_id)


def get_item_descriptions_by_name(cache_dir: str = "") -> Dict[str, Dict[str, Any]]:
    """获取按中文名索引的装备描述数据。

    返回 {中文名: {name, description, icon_url, price}}
    用于前端按名称匹配显示 tooltip。
    """
    items = get_item_descriptions(cache_dir)
    if not items:
        return {}

    result = {}
    # 使用名称索引确保不重复
    if _item_name_index:
        for name, item_id in _item_name_index.items():
            info = items.get(item_id)
            if info and info.get('description'):
                result[name] = {
                    'name': info['name'],
                    'description': info['description'],
                    'icon_url': info.get('icon_url', ''),
                    'price': info.get('price', 0),
                }
    return result


def warmup_cache(cache_dir: str = "") -> None:
    """预热装备描述缓存（服务启动时调用）。"""
    global _item_cache, _item_name_index, _building

    # 加载磁盘缓存（允许过期）
    if cache_dir:
        disk_data = _load_from_cache(cache_dir, allow_expired=True)
        if disk_data is not None:
            _item_cache = disk_data
            _item_name_index = _build_name_index(disk_data)

    # 检查是否需要刷新
    need_rebuild = False
    if _item_cache is None:
        need_rebuild = True
        log.info("[ItemDesc] 预热：无缓存数据，需要全量构建")
    else:
        now = time.time()
        expired = _count_expired()
        total = len(_item_cache)
        if expired == 0:
            log.info(f"[ItemDesc] 预热完成：缓存有效（{total} 个装备）")
        elif expired < total:
            log.info(
                f"[ItemDesc] 预热：缓存部分过期"
                f"（{expired}/{total} 过期），后台刷新"
            )
            need_rebuild = True
        else:
            log.info(f"[ItemDesc] 预热：缓存全部过期（{total} 个），后台刷新")
            need_rebuild = True

    if need_rebuild:
        with _lock:
            if _building:
                return
            _building = True
        t = threading.Thread(
            target=_build_in_background_with_retry, args=(cache_dir,), daemon=True
        )
        t.start()

    _start_checker(cache_dir)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cache_dir = os.path.join(os.path.dirname(__file__), 'apexlol_cache')
    warmup_cache(cache_dir)

    import time as _t
    for _ in range(30):
        if _item_cache and _count_expired() == 0:
            break
        _t.sleep(3)

    data = _item_cache or {}
    with_desc = sum(1 for v in data.values() if v.get('description'))
    print(f"总计: {len(data)} 个装备, {with_desc} 个有描述")

    # 按名称查找测试
    for test_name in ['无尽之刃', '灭世者的死亡之帽', '鞋子']:
        item = get_item_by_name(test_name)
        if item:
            print(f"\n{test_name}: ID={next(k for k,v in data.items() if v is item)}")
            print(f"  价格: {item.get('price', '?')}")
            print(f"  描述: {item.get('description', '')[:100]}...")
