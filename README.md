# ARAM Tool - 海克斯大乱斗智能助手

> **[English](README_EN.md)** | 中文

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)
![Docker](https://img.shields.io/badge/Docker-Ready-blue?logo=docker)
![AI](https://img.shields.io/badge/AI-Multi--Provider-orange)

基于 AI 的英雄联盟海克斯大乱斗（ARAM）Web 助手。选择英雄即可获取海克斯联动方案、强化数据、出装推荐和攻略分析。

## 功能

- **海克斯联动方案** — 基于 ApexLol 真实数据 + AI 润色，提供多套高胜率符文组合
- **海克斯强化数据** — 从 op.gg 抓取选取率、胜率，按稀有度排序
- **英雄前瞻攻略** — AI 生成完整出装、技能加点、打法要点
- **阵容分析** — 输入双方阵容，AI 生成团队策略
- **海克斯图片识别** — 上传三选一截图，AI Vision 推荐最优选择
- **陷阱警告** — 自动标注低胜率组合，避开坑位
- **多 LLM 支持** — 文本分析支持 Gemini / GLM / MiniMax / OpenAI 兼容通道
- **邀请码注册** — 管理员生成邀请链接，新用户注册时自动填入
- **Docker 一键部署** — docker compose up 即可运行

## 快速开始

### Docker 部署（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/wcwplaygitbub/lolhks.git
cd lolhks

# 2. 配置环境变量（可选）
cp config_example.py config.py
# 编辑 config.py 填入 API Key，或通过环境变量配置

# 3. 启动
docker compose up -d

# 4. 访问
# http://localhost:18081
```

首次启动会自动创建管理员账号，密码打印在容器日志中：

```bash
docker compose logs | grep "密码"
```

### 本地运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置
cp config_example.py config.py
# 编辑 config.py 填入 API Key

# 3. 启动
python -m uvicorn webui:app --host 0.0.0.0 --port 8000
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GEMINI_API_KEY` | (必填) | Gemini API 密钥 |
| `LLM_PROVIDER` | `gemini` | 文本分析模型：`gemini` / `glm` / `minimax` / `openai` |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite-preview` | Gemini 模型名 |
| `INVITE_BASE_URL` | (空) | 邀请链接前缀，如 `https://aram.example.com` |
| `ADMIN_USERNAME` | `admin` | 默认管理员用户名 |
| `ADMIN_PASSWORD` | (随机) | 默认管理员密码，不设则自动生成 |
| `AUTH_SECRET` | (随机) | Session 签名密钥 |
| `LANGUAGE` | `zh` | 界面语言：`zh` / `en` |

GLM、MiniMax、OpenAI 兼容通道的配置见 `config_example.py`。

## 项目结构

```
lolhks/
├── webui.py              # WebUI 主入口 (FastAPI)
├── auth.py               # 认证系统 (登录/注册/邀请码/管理员)
├── gemini_analyzer.py    # AI 分析模块 (多 Provider)
├── llm_provider.py       # LLM 抽象层
├── apexlol_data.py       # ApexLol 数据缓存与查询
├── apexlol_scraper.py    # ApexLol 爬虫
├── opgg_scraper.py       # op.gg 爬虫
├── lang.py               # 多语言字符串 & Prompt
├── champion_icons.py     # 英雄图标下载
├── config_example.py     # 配置模板
├── templates/
│   ├── index.html        # 主页面
│   └── login.html        # 登录/注册页面
├── static/
│   └── bg.jpg            # 背景壁纸
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── windows/              # Windows 桌面模式（已归档）
    ├── main.py
    ├── screenshot.py
    ├── lcu_client.py
    ├── launch.bat
    └── requirements.txt
```

## 数据来源

- **[ApexLol.info](https://apexlol.info)** — 海克斯符文联动方案（缓存 7 天）
- **[op.gg](https://op.gg)** — 海克斯强化选取率/胜率（缓存 2 小时）
- **[CommunityDragon](https://communitydragon.org)** — 强化元数据（图标/稀有度）
- **[Riot Data Dragon](https://developer.riotgames.com/docs/lol)** — 英雄图标

## 免责声明

- 本工具为个人学习项目，不保证分析结果准确性
- 与 Riot Games 或 League of Legends 没有官方关联
- WebUI 模式不读取/不修改任何游戏数据，仅提供参考建议
- 请遵守游戏使用条款

## License

MIT
