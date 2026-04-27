# ARAM Tool - Hextech Havoc Assistant

> English | **[中文](README.md)**

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python)
![Docker](https://img.shields.io/badge/Docker-Ready-blue?logo=docker)
![AI](https://img.shields.io/badge/AI-Multi--Provider-orange)

An AI-powered web assistant for League of Legends Hextech Havoc (ARAM). Select a champion to get augment synergy builds, pick/win rate data, item recommendations, and strategy guides.

## Features

- **Augment Synergy Builds** — Real ApexLol data + AI polish, multiple high-win-rate combinations
- **Augment Pick/Win Rates** — Fetch from op.gg, sorted by rarity and pick rate
- **Quick Champion Guide** — AI-generated full build, skill order, and playstyle tips
- **Roster Analysis** — Paste both team compositions for AI team strategy
- **Hextech Image Recognition** — Upload a 3-pick-1 screenshot, AI Vision recommends the best choice
- **Trap Warnings** — Auto-flag low win-rate combinations to avoid
- **Multi-LLM Support** — Text analysis via Gemini / GLM / MiniMax / OpenAI-compatible endpoints
- **Invite Code Registration** — Admin generates invite links, auto-filled on registration
- **Docker One-Click Deploy** — Just `docker compose up`

## Quick Start

### Docker (Recommended)

```bash
# 1. Clone
git clone https://github.com/wcwplaygitbub/lolhks.git
cd lolhks

# 2. Configure (optional)
cp config_example.py config.py
# Edit config.py with your API key, or use environment variables

# 3. Launch
docker compose up -d

# 4. Open
# http://localhost:18081
```

A default admin account is created on first launch. The password is printed in the container logs:

```bash
docker compose logs | grep "password"
```

### Local

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp config_example.py config.py
# Edit config.py with your API key

# 3. Run
python -m uvicorn webui:app --host 0.0.0.0 --port 8000
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | (required) | Gemini API key |
| `LLM_PROVIDER` | `gemini` | Text model: `gemini` / `glm` / `minimax` / `openai` |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite-preview` | Gemini model name |
| `INVITE_BASE_URL` | (empty) | Invite link prefix, e.g. `https://aram.example.com` |
| `ADMIN_USERNAME` | `admin` | Default admin username |
| `ADMIN_PASSWORD` | (random) | Default admin password, auto-generated if not set |
| `AUTH_SECRET` | (random) | Session signing secret |
| `LANGUAGE` | `zh` | UI language: `zh` / `en` |

See `config_example.py` for GLM, MiniMax, and OpenAI-compatible endpoint settings.

## Project Structure

```
lolhks/
├── webui.py              # WebUI main entry (FastAPI)
├── auth.py               # Auth system (login/register/invite/admin)
├── gemini_analyzer.py    # AI analysis module (multi-provider)
├── llm_provider.py       # LLM abstraction layer
├── apexlol_data.py       # ApexLol data cache & queries
├── apexlol_scraper.py    # ApexLol scraper
├── opgg_scraper.py       # op.gg scraper
├── lang.py               # i18n strings & prompts
├── champion_icons.py     # Champion icon downloader
├── config_example.py     # Config template
├── templates/
│   ├── index.html        # Main page
│   └── login.html        # Login/register page
├── static/
│   └── bg.jpg            # Background wallpaper
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── windows/              # Windows desktop mode (archived)
    ├── main.py
    ├── screenshot.py
    ├── lcu_client.py
    ├── launch.bat
    └── requirements.txt
```

## Data Sources

- **[ApexLol.info](https://apexlol.info)** — Hextech augment synergy data (7-day cache)
- **[op.gg](https://op.gg)** — Hextech augment pick/win rates (2-hour cache)
- **[CommunityDragon](https://communitydragon.org)** — Augment metadata (icons/rarity)
- **[Riot Data Dragon](https://developer.riotgames.com/docs/lol)** — Champion icons

## Disclaimer

- This is a personal learning project with no guarantee of accuracy
- Not affiliated with or endorsed by Riot Games or League of Legends
- WebUI mode does not read or modify any game data; it only provides reference suggestions
- Please comply with the game's terms of service

## License

MIT
