# -*- coding: utf-8 -*-
"""ARAM 助手 - 用户认证模块

特点：
- 零新依赖，全部用 Python stdlib（sqlite3 / hashlib / hmac / secrets）
- 密码 PBKDF2-SHA256 加盐（100k 迭代）
- Session 走 HMAC 签名 cookie（无服务端 session 表）
- 邀请码单次使用
- 首启自动创建管理员账号；未设 ADMIN_PASSWORD 则随机生成并打印到日志

暴露 API：
    init_auth(app)          # 挂到 FastAPI，注册中间件和 /api/auth、/api/admin 路由
    require_user(request)   # FastAPI dependency，返回当前用户 dict
    require_admin(request)  # FastAPI dependency，非管理员 403
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

log = logging.getLogger("ARAM")

# ==================== 路径与常量 ====================
_BASE_DIR = Path(__file__).parent
_DATA_DIR = _BASE_DIR / "data"
_DB_PATH = _DATA_DIR / "auth.db"
_SECRET_PATH = _DATA_DIR / "auth.secret"

COOKIE_NAME = "aram_session"
SESSION_TTL = 1 * 24 * 3600  # 1 天
PBKDF2_ITERS = 100_000

# 白名单：不需要登录的路径（前缀匹配）
_PUBLIC_PREFIXES = (
    "/login",
    "/register",
    "/api/auth/login",
    "/api/auth/register",
    "/api/config",
    "/static/",
    "/favicon.ico",
)


# ==================== 密钥 ====================

def _get_secret() -> bytes:
    env = os.environ.get("AUTH_SECRET", "").strip()
    if env:
        return env.encode()
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _SECRET_PATH.exists():
        return _SECRET_PATH.read_bytes()
    key = secrets.token_bytes(32)
    _SECRET_PATH.write_bytes(key)
    _SECRET_PATH.chmod(0o600)
    log.info(f"[Auth] 已生成 session 签名密钥 {_SECRET_PATH}")
    return key


# ==================== 数据库 ====================

def _conn() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init_schema() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                username     TEXT UNIQUE NOT NULL,
                password     TEXT NOT NULL,
                is_admin     INTEGER NOT NULL DEFAULT 0,
                created_at   INTEGER NOT NULL,
                last_login   INTEGER
            );
            CREATE TABLE IF NOT EXISTS invites (
                code         TEXT PRIMARY KEY,
                created_by   INTEGER NOT NULL REFERENCES users(id),
                created_at   INTEGER NOT NULL,
                used_by      INTEGER REFERENCES users(id),
                used_at      INTEGER
            );
            """
        )


# ==================== 密码哈希 ====================

def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return "pbkdf2_sha256${iters}${salt}${dk}".format(
        iters=PBKDF2_ITERS,
        salt=base64.b64encode(salt).decode(),
        dk=base64.b64encode(dk).decode(),
    )


def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(expected, actual)
    except Exception:
        return False


# ==================== Session Cookie ====================

def _sign_token(username: str, expires_at: int) -> str:
    payload = f"{username}|{expires_at}".encode()
    sig = hmac.new(_get_secret(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b"." + sig).decode().rstrip("=")


def _verify_token(token: str) -> Optional[str]:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded)
        payload, sig = raw.rsplit(b".", 1)
        expected = hmac.new(_get_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        username, expires_at = payload.decode().split("|")
        if int(expires_at) < int(time.time()):
            return None
        return username
    except Exception:
        return None


# ==================== 用户操作 ====================

def _get_user(username: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        row = c.execute(
            "SELECT id, username, password, is_admin, created_at, last_login "
            "FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None


def _create_user(username: str, password: str, is_admin: bool = False) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO users(username, password, is_admin, created_at) VALUES (?,?,?,?)",
            (username, _hash_password(password), 1 if is_admin else 0, int(time.time())),
        )
        return cur.lastrowid


def _touch_login(user_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (int(time.time()), user_id))


# ==================== 邀请码 ====================

def _create_invite(created_by_id: int) -> str:
    code = secrets.token_urlsafe(9)[:12]  # 12 char, url-safe
    with _conn() as c:
        c.execute(
            "INSERT INTO invites(code, created_by, created_at) VALUES (?,?,?)",
            (code, created_by_id, int(time.time())),
        )
    return code


def _consume_invite(code: str, user_id: int) -> bool:
    with _conn() as c:
        row = c.execute("SELECT used_by FROM invites WHERE code = ?", (code,)).fetchone()
        if not row or row["used_by"] is not None:
            return False
        c.execute(
            "UPDATE invites SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL",
            (user_id, int(time.time()), code),
        )
        return True


def _list_invites(limit: int = 200) -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT i.code, i.created_at, i.used_at,
                   u1.username AS created_by_name,
                   u2.username AS used_by_name
            FROM invites i
            LEFT JOIN users u1 ON u1.id = i.created_by
            LEFT JOIN users u2 ON u2.id = i.used_by
            ORDER BY i.created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _list_users() -> List[Dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, username, is_admin, created_at, last_login "
            "FROM users ORDER BY is_admin DESC, created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def _update_password(user_id: int, new_password: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE users SET password = ? WHERE id = ?",
            (_hash_password(new_password), user_id),
        )


# ==================== 管理员初始化 ====================

def _ensure_admin() -> None:
    admin_user = os.environ.get("ADMIN_USERNAME", "admin").strip() or "admin"
    with _conn() as c:
        existing = c.execute(
            "SELECT COUNT(*) AS n FROM users WHERE is_admin = 1"
        ).fetchone()["n"]
    if existing > 0:
        return
    # 没管理员，创建默认
    pwd = os.environ.get("ADMIN_PASSWORD", "").strip()
    generated = False
    if not pwd:
        pwd = secrets.token_urlsafe(12)
        generated = True
    if _get_user(admin_user):
        # 用户名被占了，给它加 admin 标记并重置密码
        with _conn() as c:
            c.execute(
                "UPDATE users SET is_admin = 1, password = ? WHERE username = ?",
                (_hash_password(pwd), admin_user),
            )
    else:
        _create_user(admin_user, pwd, is_admin=True)
    banner = (
        "\n" + "=" * 58 + "\n"
        f"[Auth] 默认管理员已创建 / 重置：\n"
        f"         用户名: {admin_user}\n"
        f"         密码  : {pwd}\n"
        + (
            "       （随机生成，只打印这一次。可通过环境变量 ADMIN_PASSWORD 指定）\n"
            if generated else ""
        )
        + "=" * 58
    )
    log.warning(banner)


# ==================== FastAPI 集成 ====================

class LoginReq(BaseModel):
    username: str
    password: str


class RegisterReq(BaseModel):
    username: str
    password: str
    invite_code: str


class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str


class ResetPasswordReq(BaseModel):
    new_password: str


def _current_user_or_none(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    username = _verify_token(token)
    if not username:
        return None
    return _get_user(username)


def require_user(request: Request) -> Dict[str, Any]:
    user = _current_user_or_none(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def require_admin(request: Request) -> Dict[str, Any]:
    user = require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def _public_path(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in _PUBLIC_PREFIXES)


def init_auth(app: FastAPI) -> None:
    """挂到 FastAPI：初始化 DB + 注册中间件和路由。"""
    _init_schema()
    _ensure_admin()

    # -------- 中间件：拦截未登录请求 --------
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if _public_path(path):
            return await call_next(request)
        user = _current_user_or_none(request)
        if user:
            return await call_next(request)
        if _wants_html(request):
            return RedirectResponse(url="/login", status_code=303)
        return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)

    # -------- 登录页 --------
    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        html = (_BASE_DIR / "templates" / "login.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{MODE}}", "login"))

    @app.get("/register", response_class=HTMLResponse)
    def register_page():
        html = (_BASE_DIR / "templates" / "login.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{MODE}}", "register"))

    # -------- 认证接口 --------
    @app.post("/api/auth/login")
    def api_login(req: LoginReq):
        user = _get_user(req.username.strip())
        if not user or not _verify_password(req.password, user["password"]):
            raise HTTPException(401, "用户名或密码错误")
        _touch_login(user["id"])
        token = _sign_token(user["username"], int(time.time()) + SESSION_TTL)
        resp = JSONResponse(
            {"ok": True, "user": {"username": user["username"], "is_admin": bool(user["is_admin"])}}
        )
        resp.set_cookie(
            COOKIE_NAME, token,
            max_age=SESSION_TTL, httponly=True, samesite="lax", path="/",
        )
        return resp

    @app.post("/api/auth/register")
    def api_register(req: RegisterReq):
        username = req.username.strip()
        if not username or len(username) < 2 or len(username) > 32:
            raise HTTPException(400, "用户名长度需在 2~32 之间")
        if len(req.password) < 6:
            raise HTTPException(400, "密码至少 6 位")
        if _get_user(username):
            raise HTTPException(409, "用户名已被占用")
        if not req.invite_code.strip():
            raise HTTPException(400, "需要邀请码")
        # 原子性：先建用户 -> 尝试消费邀请码 -> 失败则回滚
        with _conn() as c:
            row = c.execute(
                "SELECT code FROM invites WHERE code = ? AND used_by IS NULL",
                (req.invite_code.strip(),),
            ).fetchone()
            if not row:
                raise HTTPException(400, "邀请码无效或已使用")
            cur = c.execute(
                "INSERT INTO users(username, password, is_admin, created_at) VALUES (?,?,0,?)",
                (username, _hash_password(req.password), int(time.time())),
            )
            uid = cur.lastrowid
            c.execute(
                "UPDATE invites SET used_by = ?, used_at = ? WHERE code = ?",
                (uid, int(time.time()), req.invite_code.strip()),
            )
        token = _sign_token(username, int(time.time()) + SESSION_TTL)
        resp = JSONResponse({"ok": True, "user": {"username": username, "is_admin": False}})
        resp.set_cookie(
            COOKIE_NAME, token,
            max_age=SESSION_TTL, httponly=True, samesite="lax", path="/",
        )
        return resp

    @app.post("/api/auth/logout")
    def api_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/api/auth/me")
    def api_me(user: Dict[str, Any] = Depends(require_user)):
        return {
            "ok": True,
            "user": {
                "username": user["username"],
                "is_admin": bool(user["is_admin"]),
            },
        }

    @app.post("/api/auth/change_password")
    def api_change_password(
        req: ChangePasswordReq,
        user: Dict[str, Any] = Depends(require_user),
    ):
        if not _verify_password(req.old_password, user["password"]):
            raise HTTPException(401, "原密码错误")
        if len(req.new_password) < 6:
            raise HTTPException(400, "新密码至少 6 位")
        if req.new_password == req.old_password:
            raise HTTPException(400, "新密码不能与原密码相同")
        _update_password(user["id"], req.new_password)
        # 刷新 session cookie（虽然 cookie 只绑 username，不受密码影响，但保持体验一致）
        resp = JSONResponse({"ok": True})
        token = _sign_token(user["username"], int(time.time()) + SESSION_TTL)
        resp.set_cookie(
            COOKIE_NAME, token,
            max_age=SESSION_TTL, httponly=True, samesite="lax", path="/",
        )
        return resp

    # -------- 管理员接口 --------
    @app.post("/api/admin/invite")
    def api_admin_invite(user: Dict[str, Any] = Depends(require_admin)):
        code = _create_invite(user["id"])
        return {"ok": True, "code": code}

    @app.get("/api/admin/invites")
    def api_admin_invites(user: Dict[str, Any] = Depends(require_admin)):
        return {"ok": True, "invites": _list_invites()}

    @app.get("/api/admin/users")
    def api_admin_users(user: Dict[str, Any] = Depends(require_admin)):
        return {"ok": True, "users": _list_users()}

    @app.post("/api/admin/users/{user_id}/reset_password")
    def api_admin_reset_password(
        user_id: int,
        req: ResetPasswordReq,
        admin: Dict[str, Any] = Depends(require_admin),
    ):
        if len(req.new_password) < 6:
            raise HTTPException(400, "新密码至少 6 位")
        with _conn() as c:
            row = c.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, "用户不存在")
        _update_password(user_id, req.new_password)
        return {"ok": True}
