# -*- coding: utf-8 -*-
"""ARAM 助手 - AI 分析模块（多 Provider 版）

本文件名沿用 gemini_analyzer 以保持向后兼容，内部已通过 llm_provider 抽象层
支持 Gemini / GLM / MiniMax / 任意 OpenAI 兼容通道。

分析模式：
1. 极速前瞻：英雄名 → 3 套符文方案 (纯文本)
2. LCU 全局：10 人阵容 → 完整攻略       (纯文本)
3. 海克斯选择：截图 → 三选一            (Vision，始终走 Gemini)
4. 海克斯文字：OCR/手输的选项名         (纯文本)

Vision 始终使用 Gemini；文本走 config.LLM_PROVIDER 指定的模型。
"""

import logging
import time

from config import APEXLOL_ENABLED, LANGUAGE
from llm_provider import get_provider, get_vision_provider

log = logging.getLogger("ARAM")


# ==================== 海克斯符文套装玩法（WebUI 专用） ====================
def analyze_rune_builds(champion_name: str, num_builds: int = 4) -> str:
    """输入英雄名 → 基于 apexlol 缓存的真实海克斯数据生成 N 套玩法。

    流程：
    1. 确保该英雄在本地缓存中有高胜率数据（无则按需爬取）
    2. 从缓存提取 Top-N 联动方案
    3. 让 AI 仅基于这些真实海克斯组合做"润色/组织"，严禁杜撰
    """
    try:
        from config import APEXLOL_CACHE_DIR
        log.info(f"[LLM] 海克斯符文套装分析 ({champion_name}, {num_builds} 套)...")

        if not APEXLOL_ENABLED:
            return "❌ 当前配置禁用了 apexlol 数据源（config.APEXLOL_ENABLED=False），该功能不可用。"

        # ====== 强制 apexlol 真实数据：没有就按需爬取 ======
        from apexlol_data import ensure_champion_cached, extract_top_synergies
        ok, info = ensure_champion_cached(champion_name, APEXLOL_CACHE_DIR)
        if not ok:
            return (
                f"❌ 未能获取到「{champion_name}」的 apexlol 真实数据（{info}）。\n\n"
                f"可能原因：英雄名拼写问题 / 网络不可达 apexlol.info / 该英雄暂无高胜率数据。\n"
                f"请核对英雄中文名后重试。"
            )
        log.info(f"[ApexLol] 数据就绪: {info}")

        reference = extract_top_synergies(champion_name, top_n=max(num_builds + 2, 6)) or ""
        if not reference.strip():
            return f"❌ apexlol 缓存中没有「{champion_name}」的高胜率联动数据，无法生成。"

        prompt = (
            f"你是英雄联盟 **海克斯大乱斗（ARAM / Hextech Havoc）** 模式攻略分析师。\n"
            f"⚠️ 大乱斗：一条路、无野区、无打野、无对线、死后才能购物。不要出现\"对线/线上/兵线\"等词。\n\n"
            f"英雄：【{champion_name}】\n\n"
            f"🎯【权威真实数据 · 来自 apexlol.info 高胜率方案，你必须完全基于此】：\n"
            f"{reference}\n\n"
            f"【你的任务】\n"
            f"从上面真实数据中，挑选 **{num_builds} 套**差异最大的方案，改写成下面的卡片格式。\n\n"
            f"🛑【铁律 / 禁止幻觉】：\n"
            f"1. **海克斯组合**必须 100% 原样来自上方数据，一字不改；不允许杜撰、不允许意译，不允许"
            f"把英雄技能名（如\"风之障壁\"\"斩钢闪\"）写成海克斯。\n"
            f"2. 若上方数据不足 {num_builds} 套，就给出实际条数，不要硬凑。\n"
            f"3. 出装建议优先使用上方数据中的\"搭配出装\"；若没给，则基于该组合流派自行补齐为 6 件终局。\n\n"
            f"📝【输出格式】（严格 Markdown，方案之间用 `---` 分隔）：\n\n"
            f"## 🥇 方案 1：<为这套方案起一个 4~6 字的流派名>\n"
            f"- **海克斯组合**：<完全照抄数据中的核心组合，例如 速度恶魔 + 爆发伤害 + 符文学徒>\n"
            f"- **评级**：<来自数据的 S/SS/SSS 等>\n"
            f"- **流派标签**：<2~3 个短词>\n"
            f"- **核心玩法**：<2~3 句，结合该组合的实际机制说明怎么打、爆发节奏、技能循环>\n"
            f"- **优势场景**：<对什么阵容最强>\n"
            f"- **短板**：<一句话，被什么克制>\n"
            f"- **推荐出装（6 件）**：装备1 → 装备2 → 装备3 → 装备4 → 装备5 → 装备6\n"
            f"- **召唤师技能**：技能1 / 技能2\n\n"
            f"## 🥈 方案 2：...（同上结构）\n\n"
            f"...直到覆盖 {num_builds} 套（或数据上限）。\n"
        )

        t_start = time.time()
        text = get_provider().generate_text(
            [prompt], temperature=0.4, label="海克斯符文套装",
        )
        log.info(f"[LLM] 海克斯符文套装分析完成 ({time.time() - t_start:.1f}s)")

        # 把原始 apexlol 数据也附加在末尾，便于对照核验
        final = text + "\n\n---\n\n<details><summary>📎 原始 apexlol 数据（可折叠对照）</summary>\n\n" + reference + "\n\n</details>"
        return final

    except Exception as e:
        import traceback
        err = f"❌ 海克斯符文套装分析失败: {e}\n\n{traceback.format_exc()}"
        log.error(err)
        return err


# ==================== 前瞻（桌面浮窗用，保留） ====================
def analyze_champion_quick_guide(champion_name: str) -> str:
    """开局前极速前瞻：英雄名 → 数据驱动的海克斯+AI 出装（纯文本）。"""
    try:
        from lang import QUICK_GUIDE_PROMPTS
        log.info(f"[LLM] 极速前瞻分析 ({champion_name})...")

        prefilled_augments = ""
        if APEXLOL_ENABLED:
            from apexlol_data import extract_top_synergies
            prefilled_augments = extract_top_synergies(champion_name)
            if prefilled_augments:
                log.info(f"[ApexLol] 已注入符文数据 ({len(prefilled_augments)} 字符)")

        prompt = QUICK_GUIDE_PROMPTS.get(LANGUAGE, QUICK_GUIDE_PROMPTS["zh"]).format(
            champion_name=champion_name,
            prefilled_augments=prefilled_augments
            if prefilled_augments
            else "（无数据，请根据英雄特性自行推荐3套海克斯符文方案）",
        )

        t_start = time.time()
        text = get_provider().generate_text(
            [prompt], temperature=0.3, label="极速前瞻",
        )
        log.info(f"[LLM] 极速前瞻分析完成 ({time.time() - t_start:.1f}s)")

        final_output = ""
        if prefilled_augments:
            final_output += prefilled_augments + "\n\n---\n\n"
        final_output += text
        return final_output

    except Exception as e:
        import traceback
        error_msg = f"❌ 极速前瞻失败: {e}\n\n{traceback.format_exc()}"
        log.error(error_msg)
        return error_msg


# ==================== LCU 全局 ====================
def analyze_lcu_rosters(rosters: dict, hextech_history: list[str] | None = None) -> str:
    """基于 LCU 10 人阵容的全量分析。"""
    try:
        from lang import LCU_FULL_STRATEGY_PROMPTS

        my_champion = rosters.get("my_champion", "未知英雄")
        lcu_rosters = rosters.get("live_context", "")

        prefilled_augments = ""
        if APEXLOL_ENABLED:
            from apexlol_data import extract_top_synergies
            prefilled_augments = extract_top_synergies(my_champion)
            if prefilled_augments:
                log.info(f"[ApexLol] LCU 分析已附加 {my_champion} 数据 ({len(prefilled_augments)} 字符)")

        log.info(f"[LLM] 纯数据级全局分析 ({my_champion})...")
        prompt = LCU_FULL_STRATEGY_PROMPTS.get(LANGUAGE, LCU_FULL_STRATEGY_PROMPTS["zh"]).format(
            my_champion=my_champion,
            lcu_rosters=lcu_rosters,
            prefilled_augments=prefilled_augments
            if prefilled_augments
            else "（无数据，请基于知识推荐3套最强海克斯符文方案）",
        )

        if hextech_history:
            history_str = "、".join(hextech_history)
            prompt = f"📜【本局已选海克斯符文历史】: {history_str}\n" + prompt
            log.info(f"[LLM] 已注入海克斯历史 ({len(hextech_history)}个)")

        t_start = time.time()
        text = get_provider().generate_text(
            [prompt], temperature=0.4, label="全局分析",
        )
        log.info(f"[LLM] 纯数据全量分析完成 ({time.time() - t_start:.1f}s)")

        final_output = ""
        if prefilled_augments:
            final_output += prefilled_augments + "\n\n---\n\n"
        final_output += text
        return final_output

    except Exception as e:
        error_msg = f"❌ LCU 全量分析失败: {e}"
        log.error(error_msg)
        return error_msg


# ==================== 海克斯选择（Vision，始终 Gemini） ====================
def analyze_hextech_choice(png_bytes: bytes, global_context: str,
                           hextech_history: list[str], champion_name: str | None = None) -> str:
    """海克斯 3 选 1 分析（Vision，强制 Gemini）。"""
    try:
        from lang import HEXTECH_IMAGE_PROMPTS
        log.info(f"[Vision] 海克斯选择分析 (英雄: {champion_name})...")

        history_str = "、".join(hextech_history) if hextech_history else "无"

        prefilled_augments = ""
        if champion_name and APEXLOL_ENABLED:
            from apexlol_data import extract_top_synergies
            prefilled_augments = extract_top_synergies(champion_name)
            if prefilled_augments:
                log.info(f'[海克斯] 为 {champion_name} 注入高胜率"对照表"')

        prompt = HEXTECH_IMAGE_PROMPTS.get(LANGUAGE, HEXTECH_IMAGE_PROMPTS["zh"]).format(
            hextech_history=history_str,
        )

        parts: list[str] = []
        if prefilled_augments:
            parts.append(
                f"🚀【高胜率对照表】该英雄的强势海克斯如下：\n{prefilled_augments}\n\n"
                f"🛑【绝对核心指令 / 严禁幻觉】：\n"
                f"你**必须、绝对只能**从上方截图里**真实显示出来的 3 个选项**中进行三选一！\n"
                f"即使对照表里有再好的海克斯（比如'速度恶魔'等），只要**截图中没有出现**，你**绝对不可推荐**！\n"
                f"你的任务是：观察截图中的选项 -> 与对照表对比 -> 在**真正可用**的选项里挑一个最好的。\n"
                f"如果违背此项，胡乱推荐截图外的内容，将导致严重错误！"
            )
        parts.append(prompt)

        text = get_vision_provider().generate_with_image(
            png_bytes, "image/jpeg", parts,
            temperature=0.2, hard_timeout=8.0, max_retries=1, label="海克斯Vision",
        )
        log.info("[Vision] 海克斯选择分析完成")
        return text
    except Exception as e:
        return f"❌ 海克斯分析失败: {e}"


# ==================== 海克斯文字 ====================
def analyze_hextech_text(ocr_names: list[str], hextech_history: list[str],
                         champion_name: str | None = None) -> str:
    """纯文字海克斯分析：已知 3 个选项名 + ApexLol 数据 → AI 三选一。"""
    try:
        from lang import HEXTECH_TEXT_PROMPTS
        log.info(f"[LLM] 纯文字海克斯分析 (英雄: {champion_name}, 选项: {ocr_names})...")

        history_str = "、".join(hextech_history) if hextech_history else "无"

        prefilled_augments = ""
        if champion_name and APEXLOL_ENABLED:
            from apexlol_data import extract_top_synergies
            prefilled_augments = extract_top_synergies(champion_name)

        options_text = "、".join(ocr_names)
        prompt = HEXTECH_TEXT_PROMPTS.get(LANGUAGE, HEXTECH_TEXT_PROMPTS["zh"]).format(
            hextech_history=history_str,
            options_text=options_text,
        )

        effect_lines = []
        if APEXLOL_ENABLED:
            try:
                from apexlol_data import get_hextech_description
                for name in ocr_names:
                    desc = get_hextech_description(name)
                    effect_lines.append(f"- 【{name}】: {desc if desc else '(效果未知)'}")
            except Exception:
                pass

        parts: list[str] = []
        if effect_lines:
            parts.append("📋【各候选选项的真实游戏机制/效果】（供参考）\n" + "\n".join(effect_lines) + "\n")
        if prefilled_augments:
            parts.append(f"🚀【高胜率对照表】该英雄的强势海克斯如下：\n{prefilled_augments}\n\n")
        parts.append(prompt)

        text = get_provider().generate_text(
            parts, temperature=0.2, hard_timeout=5.0, max_retries=1, label="海克斯文字",
        )
        log.info("[LLM] 纯文字海克斯分析完成")
        return text
    except Exception as e:
        return f"❌ 海克斯分析失败: {e}"
