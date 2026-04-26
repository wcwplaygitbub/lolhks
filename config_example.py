# -*- coding: utf-8 -*-
"""ARAM 助手 - 配置文件 (模板)"""

import os
import sys

# ==================== 语言配置 ====================
# "zh" = 中文 (Chinese)
# "en" = English
LANGUAGE = "zh"

# ==================== LLM Provider 选择 ====================
# 文本分析使用哪个模型："gemini" | "glm" | "minimax" | "openai"
# 注意：海克斯图片识别（Vision）始终走 Gemini，与此配置无关
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini")

# ==================== Gemini API 配置 ====================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_API_KEY_HERE")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite-preview")

# ==================== 智谱 GLM 配置（OpenAI 兼容） ====================
GLM_API_KEY = os.environ.get("GLM_API_KEY", "")
GLM_MODEL = os.environ.get("GLM_MODEL", "glm-4-flash")
GLM_BASE_URL = os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")

# ==================== MiniMax 配置（OpenAI 兼容） ====================
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "abab6.5s-chat")
MINIMAX_BASE_URL = os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.chat/v1")

# ==================== 通用 OpenAI 兼容通道 (DeepSeek / Qwen / Ollama 等) ====================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

# ==================== 热键配置 ====================
TOGGLE_HOTKEY = "Ctrl+F12"    # 切换悬浮窗显示/隐藏

# ==================== UI 配置 ====================
OVERLAY_BG_COLOR = "#1a1a2e"
OVERLAY_FG_COLOR = "#e0e0e0"
OVERLAY_ACCENT_COLOR = "#00d4ff"
OVERLAY_TITLE_COLOR = "#ffd700"
OVERLAY_WIDTH = 520
OVERLAY_MAX_HEIGHT = 750
OVERLAY_FONT_FAMILY = "Microsoft YaHei UI"
OVERLAY_FONT_SIZE = 11
OVERLAY_OPACITY = 0.92

# ==================== 截图配置 ====================
SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots")

# ==================== ApexLol 数据增强 ====================
APEXLOL_ENABLED = True                 
APEXLOL_CACHE_DIR = os.path.join(os.path.dirname(__file__), "apexlol_cache")
APEXLOL_CACHE_TTL_DAYS = 7             
