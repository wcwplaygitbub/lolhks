# -*- coding: utf-8 -*-
"""ARAM 助手 - WebUI (FastAPI)

运行：
    uvicorn webui:app --host 0.0.0.0 --port 8000

访问: http://localhost:8000
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from gemini_analyzer import (
    analyze_rune_builds,
    analyze_champion_quick_guide,
    analyze_lcu_rosters,
    analyze_hextech_choice,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ARAM")

app = FastAPI(title="ARAM AI 助手")

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"

# ==================== 认证 ====================
from auth import init_auth  # noqa: E402
init_auth(app)


# ==================== 页面 ====================
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/config")
def api_config():
    """暴露当前 Provider 选择供前端显示。"""
    from llm_provider import _cfg
    return {
        "llm_provider": _cfg("LLM_PROVIDER", "gemini"),
        "language": _cfg("LANGUAGE", "zh"),
        "invite_base_url": _cfg("INVITE_BASE_URL", ""),
    }


@app.get("/api/champions")
def api_champions():
    """返回英雄列表 + 本地图标 URL（/static/champions/{id}.png）。"""
    from champion_icons import ensure_champion_icons
    return {"ok": True, "champions": ensure_champion_icons()}


@app.on_event("startup")
def _warmup_icons():
    """首次启动时拉取图标，后续命中本地缓存。"""
    try:
        from champion_icons import ensure_champion_icons
        ensure_champion_icons()
    except Exception as e:
        log.warning(f"champion icons warmup failed: {e}")


# ==================== 海克斯符文套装玩法（直连 apexlol，无 AI） ====================
class RuneDataReq(BaseModel):
    champion: str
    num_builds: int = 6


@app.post("/api/rune_data")
def api_rune_data(req: RuneDataReq):
    """直接从 apexlol 数据返回该英雄的高胜率符文套装，不走 AI。"""
    name = (req.champion or "").strip()
    if not name:
        raise HTTPException(400, "champion 不能为空")
    n = max(3, min(req.num_builds or 6, 12))
    try:
        from apexlol_data import ensure_champion_cached, extract_top_synergies_json
        ok, info = ensure_champion_cached(name, config.APEXLOL_CACHE_DIR)
        if not ok:
            return JSONResponse({"ok": False, "error": f"获取数据失败：{info}"}, status_code=404)
        data = extract_top_synergies_json(name, top_n=n)
        if not data.get("builds") and not data.get("trap_warnings"):
            return JSONResponse({"ok": False, "error": f"暂无「{name}」的联动数据"}, status_code=404)
        return {"ok": True, "source": info, **data}
    except Exception as e:
        log.exception("rune_data failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ==================== op.gg 海克斯大乱斗（ARAM Mayhem）构筑 ====================
class OpggReq(BaseModel):
    champion: str


@app.post("/api/augments")
def api_opgg_mayhem(req: OpggReq):
    """从 op.gg 官方 JSON API 抓取海克斯大乱斗数据（出装+增幅+召唤师技能）。"""
    name = (req.champion or "").strip()
    if not name:
        raise HTTPException(400, "champion 不能为空")
    try:
        from opgg_scraper import fetch_aram_mayhem
        from apexlol_data import load_cache
        load_cache(config.APEXLOL_CACHE_DIR)
        data = fetch_aram_mayhem(name)
        if not data.get("ok"):
            return JSONResponse(data, status_code=404)
        return data
    except Exception as e:
        log.exception("opgg_mayhem failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ==================== 海克斯符文套装玩法（AI 润色版，保留） ====================
class RuneBuildsReq(BaseModel):
    champion: str
    num_builds: int = 4


@app.post("/api/rune_builds")
def api_rune_builds(req: RuneBuildsReq):
    name = (req.champion or "").strip()
    if not name:
        raise HTTPException(400, "champion 不能为空")
    n = max(2, min(req.num_builds or 4, 6))
    try:
        text = analyze_rune_builds(name, num_builds=n)
        return {"ok": True, "markdown": text}
    except Exception as e:
        log.exception("rune_builds failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ==================== 英雄前瞻（完整攻略，保留） ====================
class QuickGuideReq(BaseModel):
    champion: str


@app.post("/api/quick_guide")
def api_quick_guide(req: QuickGuideReq):
    name = (req.champion or "").strip()
    if not name:
        raise HTTPException(400, "champion 不能为空")
    try:
        text = analyze_champion_quick_guide(name)
        return {"ok": True, "markdown": text}
    except Exception as e:
        log.exception("quick_guide failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ==================== LCU 阵容粘贴分析 ====================
class LcuReq(BaseModel):
    my_champion: str
    rosters_text: str  # 用户粘贴的阵容信息，如 "我方: 亚索/盲僧/... 敌方: 劫/..."
    hextech_history: Optional[list[str]] = None


@app.post("/api/lcu_rosters")
def api_lcu_rosters(req: LcuReq):
    if not req.my_champion or not req.rosters_text:
        raise HTTPException(400, "my_champion 与 rosters_text 均为必填")
    try:
        rosters = {
            "my_champion": req.my_champion.strip(),
            "live_context": req.rosters_text.strip(),
        }
        text = analyze_lcu_rosters(rosters, req.hextech_history or [])
        return {"ok": True, "markdown": text}
    except Exception as e:
        log.exception("lcu_rosters failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ==================== 海克斯图片上传分析 ====================
@app.post("/api/hextech")
async def api_hextech(
    image: UploadFile = File(...),
    champion: str = Form(""),
    history: str = Form(""),  # 逗号分隔
):
    try:
        png_bytes = await image.read()
        if not png_bytes:
            raise HTTPException(400, "图片为空")
        hist_list = [x.strip() for x in history.split(",") if x.strip()] if history else []
        text = analyze_hextech_choice(
            png_bytes=png_bytes,
            global_context="",
            hextech_history=hist_list,
            champion_name=champion.strip() or None,
        )
        return {"ok": True, "markdown": text}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("hextech failed")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ==================== 可选：静态资源 ====================
_static_dir = BASE_DIR / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webui:app", host="0.0.0.0", port=8000, reload=False)
