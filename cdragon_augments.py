# -*- coding: utf-8 -*-
"""ARAM 助手 - 海克斯强化描述数据

数据源优先级：
  1. ApexLol — 天赋列表 + 详情页（中文描述、中文名、阶级）为主
  2. CommunityDragon cherry-augments.json — 补充：天赋 ID / 英文名 / 图标 / 稀有度
  3. LoL Wiki — 回退补充仍缺失的描述

缓存策略：
  - 每个天赋独立缓存时间 (cached_at)，2 小时 TTL
  - 启动时预热所有天赋（含重试 + 过时缓存兜底）
  - 后台定期检查过期天赋，少量过期逐个刷新，大量过期全量刷新
  - API 调用只读缓存；缺失的少量天赋触发后台逐个补拉
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

_CDRAGON_DEFAULT = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/default/v1/cherry-augments.json"
)
_CDRAGON_ZH_CN = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/zh_cn/v1/cherry-augments.json"
)
_CDRAGON_ASSET_BASE = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default"
)
_WIKI_API = "https://wiki.leagueoflegends.com/en-us/api.php"
_WIKI_MODULE = "Module:Sandbox/WordlessMeteor/ArenaAugmentData/zh-CN/data"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain",
}

# ---- 缓存参数 ----
_AUG_CACHE_TTL = 2 * 60 * 60   # 单个天赋缓存 TTL：2 小时
_CHECK_INTERVAL = 30 * 60       # 定期检查间隔：30 分钟
_MAX_RETRIES = 2                # 启动预热最大重试次数
_INDIVIDUAL_FETCH_LIMIT = 10    # 单次后台逐个补拉上限

# ---- 全局状态 ----
_aug_cache: Optional[Dict[int, Dict[str, Any]]] = None  # {id: {name, name_en, description, icon_url, rarity, cached_at}}
_lock = threading.Lock()
_building = False
_checker_started = False
_last_individual_fetch_ts: float = 0  # 上次逐个补拉时间（防止频繁触发）
_INDIVIDUAL_FETCH_COOLDOWN = 10 * 60  # 逐个补拉冷却：10 分钟


# ==================== CommunityDragon 数据 ====================

def _fetch_cdragon_zh_cn() -> Dict[int, Dict[str, str]]:
    """获取 CDragon 中文版本，返回 {id: {name, icon_url, rarity}}。"""
    try:
        data = requests.get(_CDRAGON_ZH_CN, headers=_HEADERS, timeout=15).json()
        result = {}
        for aug in data:
            icon_path = (aug.get('augmentSmallIconPath') or '').replace(
                '/lol-game-data/assets/', ''
            ).lower()
            result[int(aug['id'])] = {
                'name': aug.get('nameTRA', ''),
                'icon_url': f"{_CDRAGON_ASSET_BASE}/{icon_path}" if icon_path else '',
                'rarity': aug.get('rarity', ''),
            }
        return result
    except Exception as e:
        log.warning(f"[AugDesc] CDragon zh_cn 数据获取失败: {e}")
        return {}


def _fetch_cdragon_default() -> Dict[int, str]:
    """获取 CDragon default 版本（英文），返回 {id: english_name}。"""
    try:
        data = requests.get(_CDRAGON_DEFAULT, headers=_HEADERS, timeout=15).json()
        return {aug['id']: aug.get('nameTRA', '') for aug in data}
    except Exception as e:
        log.warning(f"[AugDesc] CDragon default 数据获取失败: {e}")
        return {}


# ==================== ApexLol 数据 ====================

def _strip_tier_prefix(name: str) -> str:
    """去掉 ApexLol 名称的阶级前缀（如"黄金阶罪恶快感" → "罪恶快感"）。"""
    for prefix in ('棱彩阶', '黄金阶', '白银阶', '青铜阶'):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _fetch_apexlol_descriptions() -> Dict[str, str]:
    """从 ApexLol 抓取所有海克斯天赋的中文描述。

    返回 {中文名(无阶级前缀): 描述文本}。
    """
    try:
        from apexlol_scraper import get_hextech_list, scrape_hextech_detail

        hex_list = get_hextech_list()
        if not hex_list:
            log.warning("[AugDesc] ApexLol 天赋列表为空")
            return {}

        descriptions = {}
        total = len(hex_list)
        for i, hex_info in enumerate(hex_list):
            hex_id = hex_info['id']
            hex_name = hex_info['name']

            try:
                detail = scrape_hextech_detail(hex_id)
                if detail and detail.get('description'):
                    display_name = _strip_tier_prefix(detail.get('name', hex_name))
                    descriptions[display_name] = detail['description']
            except Exception:
                pass

            if i % 50 == 0:
                log.info(f"[AugDesc] ApexLol 进度: {i+1}/{total}")
            if i < total - 1:
                time.sleep(0.3)

        log.info(f"[AugDesc] ApexLol 描述获取完成: {len(descriptions)} 条")
        return descriptions
    except Exception as e:
        log.warning(f"[AugDesc] ApexLol 数据获取失败: {e}")
        return {}


# ==================== Wiki 回退数据 ====================

def _strip_wiki_markup(text: str) -> str:
    """将 wiki markup 转为可读纯文本。"""
    if not text:
        return ""
    # {{as|text|stat}} → text
    text = re.sub(r'\{\{as\|([^|}]+)(?:\|[^|}]*)*\}\}', r'\1', text)
    # {{tip|type|display}} → display
    def _tip_repl(m):
        parts = m.group(1).split('|')
        return parts[-1] if len(parts) > 1 else parts[0]
    text = re.sub(r'\{\{tip\|([^}]+)\}\}', _tip_repl, text)
    # {{pp|val1 to val2}} → val1 ~ val2
    text = re.sub(r'\{\{pp\|([^}]+)\}\}', lambda m: m.group(1).replace(' to ', ' ~ '), text)
    # {{fd|val}} → val
    text = re.sub(r'\{\{fd\|([^}]+)\}\}', r'\1', text)
    # {{ii|name|display}} → display
    text = re.sub(r'\{\{ii\|([^}]+)\}\}', lambda m: m.group(1).split('|')[-1], text)
    # {{g|val}} → val
    text = re.sub(r'\{\{g\|([^}]+)\}\}', r'\1', text)
    # {{sbc|text}} → text
    text = re.sub(r'\{\{sbc\|([^}]+)\}\}', r'\1', text)
    # {{ci|name|display}} → display
    text = re.sub(r'\{\{ci\|([^}]+)\}\}', lambda m: m.group(1).split('|')[-1], text)
    # {{ai|name|champ|display}} → display
    text = re.sub(r'\{\{ai\|([^}]+)\}\}', lambda m: m.group(1).split('|')[-1], text)
    # {{sti|stat|display}} → display
    text = re.sub(r'\{\{sti\|([^}]+)\}\}', lambda m: m.group(1).split('|')[-1], text)
    # {{nie|name|display}} → display
    text = re.sub(r'\{\{nie\|([^}]+)\}\}', lambda m: m.group(1).split('|')[-1], text)
    # {{rutngt|val}} → val
    text = re.sub(r'\{\{rutngt\|([^}]+)\}\}', r'\1', text)
    # {{er|...}} → remove
    text = re.sub(r'\{\{er[^}]*\}\}', '', text)
    # 通用兜底：{{xxx|...}} → 最后一个 | 后的内容
    text = re.sub(r'\{\{([^}]+)\}\}', lambda m: m.group(1).split('|')[-1].strip(), text)
    # [[File:...]] → remove
    text = re.sub(r'\[\[File:[^\]]*\]\]', '', text)
    # [[link|display]] → display
    text = re.sub(r'\[\[([^\]]+)\]\]', lambda m: m.group(1).split('|')[-1], text)
    # '''bold''' → bold
    text = re.sub(r"'''([^']+)'''", r'\1', text)
    # ''italic'' → italic
    text = re.sub(r"''([^']+)''", r'\1', text)
    # <br> → newline
    text = re.sub(r'<br\s*/?>', '\n', text)
    # HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Clean up
    text = re.sub(r'[ \t]+', ' ', text).strip()
    return text


def _fetch_wiki_descriptions() -> Dict[str, str]:
    """从 LoL Wiki 获取中文描述作为回退数据。

    返回 {小写英文名 or 中文名: 清理后的描述}。
    """
    try:
        import subprocess
        url = (
            f"{_WIKI_API}?action=query"
            f"&titles={requests.utils.quote(_WIKI_MODULE)}"
            f"&prop=revisions&rvprop=content&format=json"
        )
        result = subprocess.run(
            ['curl', '-s', '-H', 'User-Agent: Mozilla/5.0', url],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        pages = data.get('query', {}).get('pages', {})
        if not pages:
            return {}
        page = next(iter(pages.values()))
        revisions = page.get('revisions', [])
        if not revisions:
            return {}
        content = revisions[0]['*']

        lookup = {}
        pattern = re.compile(r'\["([^"]+)"\]\s*=\s*\{\s*(.*?)\},', re.DOTALL)
        for m in pattern.finditer(content):
            en_name = m.group(1)
            body = m.group(2)
            name_tra_m = re.search(r'\["nameTra"\]\s*=\s*"((?:[^"\\]|\\.)*)"', body)
            cn_name = name_tra_m.group(1).replace('\\"', '"') if name_tra_m else ''
            desc_m = re.search(r'\["description"\]\s*=\s*"((?:[^"\\]|\\.)*)"', body)
            if not desc_m:
                continue
            desc_raw = desc_m.group(1).replace('\\"', '"')
            desc = _strip_wiki_markup(desc_raw)
            if desc:
                if en_name:
                    lookup[en_name.lower()] = desc
                if cn_name:
                    lookup[cn_name] = desc

        log.info(f"[AugDesc] Wiki 回退数据加载: {len(lookup)} 条")
        return lookup
    except Exception as e:
        log.warning(f"[AugDesc] Wiki 回退数据获取失败: {e}")
        return {}


# ==================== 合并数据 ====================

def _build_augment_data() -> Dict[int, Dict[str, Any]]:
    """全量构建海克斯天赋描述数据（启动预热 + 全量刷新时调用）。

    数据源优先级：
      1. ApexLol → 中文描述（主）+ 中文名
      2. CDragon zh_cn → ID / 中文名 / 图标 / 稀有度
      3. CDragon default → ID / 英文名
      4. Wiki → 回退补充仍缺失的描述

    每个天赋独立记录 cached_at 时间戳。
    """
    # 1. 从 ApexLol 获取描述数据
    apex_descs = _fetch_apexlol_descriptions()

    # 2. 从 CDragon 获取基础数据
    zh_data = _fetch_cdragon_zh_cn()
    if not zh_data:
        log.warning("[AugDesc] CDragon 中文数据为空，无法构建")
        return {}

    # 3. 从 CDragon 获取英文数据
    en_data = _fetch_cdragon_default()

    # 4. 合并：ApexLol 描述 + CDragon 元数据
    now = time.time()
    result = {}
    matched = 0
    for aug_id, zh_info in zh_data.items():
        cn_name = zh_info['name']
        en_name = en_data.get(aug_id, '')
        desc = apex_descs.get(cn_name, '')

        if desc:
            matched += 1

        result[aug_id] = {
            'name': cn_name,
            'name_en': en_name,
            'description': desc,
            'icon_url': zh_info['icon_url'],
            'rarity': zh_info['rarity'],
            'cached_at': now,
        }

    log.info(
        f"[AugDesc] 数据合并完成: {len(result)} 个天赋, "
        f"{matched} 个有描述 ({len(result) - matched} 个无描述)"
    )

    # 5. Wiki 回退补充
    missing = {aid: info for aid, info in result.items() if not info['description']}
    if missing:
        log.info(f"[AugDesc] 尝试从 Wiki 回退补充 {len(missing)} 个缺失描述")
        wiki_descs = _fetch_wiki_descriptions()
        wiki_matched = 0
        for aid, info in missing.items():
            en_name = info['name_en']
            cn_name = info['name']
            wiki_desc = ''
            if en_name:
                wiki_desc = wiki_descs.get(en_name.lower(), '')
            if not wiki_desc and cn_name:
                wiki_desc = wiki_descs.get(cn_name, '')
            if wiki_desc:
                result[aid]['description'] = wiki_desc
                wiki_matched += 1
        if wiki_matched:
            log.info(f"[AugDesc] Wiki 回退补充了 {wiki_matched} 个描述")

    final_matched = sum(1 for v in result.values() if v['description'])
    log.info(
        f"[AugDesc] 最终数据: {len(result)} 个天赋, "
        f"{final_matched} 个有描述 ({len(result) - final_matched} 个无描述)"
    )
    return result


# ==================== 磁盘缓存 ====================

_CACHE_FILENAME = "augment_descriptions.json"


def _cache_path(cache_dir: str) -> Path:
    return Path(cache_dir) / _CACHE_FILENAME


def _load_from_cache(cache_dir: str, allow_expired: bool = False) -> Optional[Dict]:
    """从本地 JSON 缓存加载数据。

    Args:
        allow_expired: True 时即使过期也返回数据（启动兜底用）。
                       False 时过期返回 None。
    """
    p = _cache_path(cache_dir)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding='utf-8'))
        data = obj.get('augments', {})
        if not data:
            return None

        # 兼容旧格式：全局 updated_at → 逐个 cached_at
        global_updated = obj.get('updated_at', 0)
        for aug_id_str, info in data.items():
            if 'cached_at' not in info:
                info['cached_at'] = global_updated

        if not allow_expired:
            # 检查是否有过期的天赋（如果有任何过期则返回 None 触发刷新）
            now = time.time()
            max_cached = max((info.get('cached_at', 0) for info in data.values()), default=0)
            if now - max_cached > _AUG_CACHE_TTL:
                log.info("[AugDesc] 磁盘缓存已过期")
                return None

        log.info(f"[AugDesc] 磁盘缓存加载: {len(data)} 条{'（允许过期）' if allow_expired else ''}")
        return data
    except Exception as e:
        log.warning(f"[AugDesc] 缓存读取失败: {e}")
        return None


def _save_to_cache(cache_dir: str, data: Dict) -> None:
    """保存到本地 JSON 缓存。"""
    p = _cache_path(cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {'augments': data}
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding='utf-8')
    log.info(f"[AugDesc] 缓存已写入: {p} ({len(data)} 条)")


# ==================== 逐个刷新 ====================

def _build_apexlol_name_map() -> Dict[str, str]:
    """构建 ApexLol 中文名(无阶级前缀) → hex_id 映射。"""
    try:
        from apexlol_scraper import get_hextech_list
        hex_list = get_hextech_list()
        name_map = {}
        for h in hex_list:
            clean_name = _strip_tier_prefix(h.get('name', ''))
            if clean_name:
                name_map[clean_name] = h['id']
        return name_map
    except Exception as e:
        log.warning(f"[AugDesc] 获取 ApexLol 映射失败: {e}")
        return {}


def _refresh_individual_augments(aug_ids: List[int], cache_dir: str) -> None:
    """逐个从 ApexLol 补拉指定天赋的描述（少量缺失时使用）。

    最多拉取 _INDIVIDUAL_FETCH_LIMIT 个，避免大量爬取。
    """
    if not aug_ids or _aug_cache is None:
        return

    # 限制数量
    to_fetch = aug_ids[:_INDIVIDUAL_FETCH_LIMIT]
    if len(aug_ids) > _INDIVIDUAL_FETCH_LIMIT:
        log.info(
            f"[AugDesc] 缺失天赋 {len(aug_ids)} 个，"
            f"本次仅补拉前 {_INDIVIDUAL_FETCH_LIMIT} 个"
        )

    log.info(f"[AugDesc] 尝试逐个补拉 {len(to_fetch)} 个天赋描述")

    # 获取 ApexLol 名称映射
    name_map = _build_apexlol_name_map()
    if not name_map:
        log.warning("[AugDesc] ApexLol 名称映射为空，无法逐个补拉")
        return

    refreshed = 0
    now = time.time()
    for aug_id in to_fetch:
        info = _aug_cache.get(aug_id)
        if not info:
            continue
        cn_name = info.get('name', '')
        hex_id = name_map.get(cn_name)
        if not hex_id:
            continue

        try:
            from apexlol_scraper import scrape_hextech_detail
            detail = scrape_hextech_detail(hex_id)
            if detail and detail.get('description'):
                _aug_cache[aug_id]['description'] = detail['description']
                _aug_cache[aug_id]['cached_at'] = now
                refreshed += 1
            time.sleep(0.3)
        except Exception as e:
            log.debug(f"[AugDesc] 补拉天赋 {cn_name}({aug_id}) 失败: {e}")

    if refreshed > 0:
        log.info(f"[AugDesc] 逐个补拉完成: {refreshed}/{len(to_fetch)} 成功")
        if cache_dir:
            _save_to_cache(cache_dir, _aug_cache)
    else:
        log.warning(f"[AugDesc] 逐个补拉: 0/{len(to_fetch)} 成功")


# ==================== 后台构建（含重试） ====================

def _build_in_background_with_retry(cache_dir: str) -> None:
    """后台全量构建，带重试。所有重试失败后保留过时缓存。"""
    global _aug_cache, _building

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            log.info(f"[AugDesc] 全量构建 (尝试 {attempt}/{_MAX_RETRIES})...")
            data = _build_augment_data()
            if data:
                _aug_cache = data
                if cache_dir:
                    _save_to_cache(cache_dir, data)
                log.info(f"[AugDesc] 全量构建成功: {len(data)} 个天赋")
                _building = False
                return
            log.warning(f"[AugDesc] 全量构建返回空数据 (尝试 {attempt}/{_MAX_RETRIES})")
        except Exception as e:
            log.error(f"[AugDesc] 全量构建异常 (尝试 {attempt}/{_MAX_RETRIES}): {e}")

        if attempt < _MAX_RETRIES:
            wait = 10 * attempt
            log.info(f"[AugDesc] 等待 {wait}s 后重试...")
            time.sleep(wait)

    # 所有重试均失败
    _building = False
    if _aug_cache:
        log.error(
            f"[AugDesc] ⚠️ 所有重试均失败，继续使用过时缓存数据"
            f"（{_count_expired()} 个已过期，{len(_aug_cache)} 个总计）"
        )
    else:
        log.error("[AugDesc] ⚠️ 所有重试均失败，无任何缓存数据可用")


def _count_expired() -> int:
    """统计当前缓存中过期的天赋数量。"""
    if _aug_cache is None:
        return 0
    now = time.time()
    return sum(1 for info in _aug_cache.values()
               if now - info.get('cached_at', 0) > _AUG_CACHE_TTL)


# ==================== 定期检查 ====================

def _periodic_checker(cache_dir: str) -> None:
    """后台线程：定期检查过期天赋并按需刷新。"""
    while True:
        time.sleep(_CHECK_INTERVAL)
        try:
            _check_and_refresh(cache_dir)
        except Exception as e:
            log.error(f"[AugDesc] 定期检查异常: {e}")


def _check_and_refresh(cache_dir: str) -> None:
    """检查过期天赋，少量逐个刷新，大量全量刷新。"""
    global _building

    if _aug_cache is None or _building:
        return

    now = time.time()
    expired_ids: List[int] = []
    missing_desc_ids: List[int] = []

    for aug_id, info in _aug_cache.items():
        cached_at = info.get('cached_at', 0)
        if not info.get('description'):
            missing_desc_ids.append(aug_id)
        elif now - cached_at > _AUG_CACHE_TTL:
            expired_ids.append(aug_id)

    if not expired_ids and not missing_desc_ids:
        return

    total = len(_aug_cache)
    expired_ratio = len(expired_ids) / total if total else 0

    log.info(
        f"[AugDesc] 定期检查: {len(expired_ids)}/{total} 个过期, "
        f"{len(missing_desc_ids)} 个无描述"
    )

    # 过期比例高 → 全量刷新
    if expired_ratio > 0.5:
        log.info("[AugDesc] 过期比例较高，执行全量刷新")
        with _lock:
            if _building:
                return
            _building = True
        t = threading.Thread(
            target=_build_in_background_with_retry, args=(cache_dir,), daemon=True
        )
        t.start()
        return

    # 少量过期 + 缺失 → 逐个刷新
    refresh_ids = expired_ids + missing_desc_ids
    if refresh_ids:
        log.info(f"[AugDesc] 尝试逐个刷新 {len(refresh_ids)} 个天赋")
        t = threading.Thread(
            target=_refresh_individual_augments,
            args=(refresh_ids, cache_dir),
            daemon=True,
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
    log.info(f"[AugDesc] 定期检查线程已启动（间隔 {_CHECK_INTERVAL // 60} 分钟）")


# ==================== 公共接口 ====================

def get_augment_descriptions(cache_dir: str = "") -> Dict[int, Dict[str, Any]]:
    """获取海克斯天赋描述数据。

    优先从内存缓存读取；缺失的少量天赋触发后台逐个补拉（不阻塞）。
    补拉有冷却时间，避免每个 API 请求都触发爬取。

    返回 {id: {name, name_en, description, icon_url, rarity, cached_at}}
    """
    global _aug_cache, _building, _last_individual_fetch_ts

    # 1. 内存缓存
    if _aug_cache is not None:
        # 检查是否有缺失描述的天赋，少量则后台补拉（有冷却）
        missing = [
            aid for aid, info in _aug_cache.items()
            if not info.get('description') and info.get('name')
        ]
        now = time.time()
        if (missing and not _building
                and now - _last_individual_fetch_ts > _INDIVIDUAL_FETCH_COOLDOWN):
            _last_individual_fetch_ts = now
            t = threading.Thread(
                target=_refresh_individual_augments,
                args=(missing, cache_dir),
                daemon=True,
            )
            t.start()
        return _aug_cache

    # 2. 磁盘缓存（允许过期，作为兜底）
    if cache_dir:
        disk_data = _load_from_cache(cache_dir, allow_expired=True)
        if disk_data is not None:
            _aug_cache = disk_data
            return _aug_cache

    # 3. 无数据
    return {}


def warmup_cache(cache_dir: str = "") -> None:
    """预热天赋描述缓存（服务启动时调用）。

    1. 加载磁盘缓存（即使过期也加载，作为兜底）
    2. 缓存新鲜 → 直接使用
    3. 缓存过期或不存在 → 后台全量构建（含重试），构建失败保留过时数据
    4. 启动定期检查线程
    """
    global _aug_cache, _building

    # 1. 加载磁盘缓存（允许过期）
    if cache_dir:
        disk_data = _load_from_cache(cache_dir, allow_expired=True)
        if disk_data is not None:
            _aug_cache = disk_data

    # 2. 检查是否需要刷新
    need_rebuild = False
    if _aug_cache is None:
        need_rebuild = True
        log.info("[AugDesc] 预热：无缓存数据，需要全量构建")
    else:
        now = time.time()
        expired = _count_expired()
        total = len(_aug_cache)
        if expired == 0:
            log.info(f"[AugDesc] 预热完成：缓存有效（{total} 个天赋）")
        elif expired < total:
            fresh_pct = int((total - expired) / total * 100)
            log.info(
                f"[AugDesc] 预热：缓存部分过期"
                f"（{expired}/{total} 过期，{fresh_pct}% 有效），后台刷新"
            )
            need_rebuild = True
        else:
            log.info(f"[AugDesc] 预热：缓存全部过期（{total} 个），后台刷新")
            need_rebuild = True

    # 3. 需要刷新 → 后台全量构建（含重试）
    if need_rebuild:
        with _lock:
            if _building:
                return
            _building = True
        t = threading.Thread(
            target=_build_in_background_with_retry, args=(cache_dir,), daemon=True
        )
        t.start()

    # 4. 启动定期检查线程
    _start_checker(cache_dir)


# ==================== 命令行测试 ====================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    cache_dir = os.path.join(os.path.dirname(__file__), 'apexlol_cache')
    warmup_cache(cache_dir)

    # 等待缓存构建完成
    import time as _t
    for _ in range(60):
        if _aug_cache and _count_expired() == 0:
            break
        _t.sleep(5)

    data = _aug_cache or {}
    shown = 0
    for aug_id, info in sorted(data.items()):
        if info.get('description'):
            print(f"\n#{aug_id} {info['name']} ({info['name_en']}) [{info['rarity']}]")
            print(f"  描述: {info['description'][:100]}...")
            shown += 1
            if shown >= 5:
                break
    print(f"\n总计: {len(data)} 个天赋, 有描述: {sum(1 for v in data.values() if v.get('description'))}")
