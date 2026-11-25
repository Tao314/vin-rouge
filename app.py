"""vin-rouge v2: 酒类分销进销存 (FastAPI + SQLite, 单文件)."""
import hashlib
import json
import os
import secrets
import subprocess
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, timedelta, date
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("VIN_ROUGE_DB", str(BASE_DIR / "vin_rouge.db")))
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
LOW_STOCK_THRESHOLD = 5
TOKEN_TTL_HOURS = 24


# ---------- 版本（单点管理） ----------
# 唯一版本源：只改 APP_VERSION，全仓同步
# （前端页脚、README、/api/version、/api/config 都以它为准，不要在别处手写版本号）。
APP_VERSION = "v2.5.0"
BUILD_DATE = "2026-11-25"


def _git_commit():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(BASE_DIR),
            stderr=subprocess.DEVNULL, timeout=3).decode().strip()
    except Exception:
        return ""

app = FastAPI(title="vin-rouge v2", version=APP_VERSION)


# ---------- DB ----------
def conn():
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
          password_hash TEXT NOT NULL, active INTEGER DEFAULT 1,
          must_change_password INTEGER DEFAULT 0, session_token TEXT,
          session_expires_at TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS audit_logs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT, action TEXT,
          target TEXT, note TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS wines(
          id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT UNIQUE, name TEXT NOT NULL,
          brand TEXT, spec TEXT, capacity_ml INTEGER, abv REAL, origin TEXT,
          manufacturer TEXT, image_path TEXT, description TEXT,
          created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS warehouses(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS suppliers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          phone TEXT, address TEXT, note TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS customers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
          phone TEXT, address TEXT, note TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS stock(
          warehouse_id INTEGER, wine_id INTEGER, batch TEXT DEFAULT '',
          quantity INTEGER DEFAULT 0,
          PRIMARY KEY(warehouse_id, wine_id, batch));
        CREATE TABLE IF NOT EXISTS inbound_orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT UNIQUE NOT NULL,
          type TEXT NOT NULL, supplier_id INTEGER, warehouse_id INTEGER,
          order_date TEXT, carrier TEXT, tracking_no TEXT, freight REAL DEFAULT 0,
          freight_free INTEGER DEFAULT 0, freight_payer TEXT, goods_total REAL DEFAULT 0,
          pay_status TEXT DEFAULT 'unpaid', note TEXT, created_at TEXT,
          status TEXT DEFAULT 'pending',
          voided INTEGER DEFAULT 0, voided_at TEXT);
        CREATE TABLE IF NOT EXISTS inbound_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, wine_id INTEGER,
          batch TEXT DEFAULT '', qty INTEGER, unit_price REAL,
          confirmed_qty INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS outbound_orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, order_no TEXT UNIQUE NOT NULL,
          customer_id INTEGER, warehouse_id INTEGER, order_date TEXT,
          carrier TEXT, tracking_no TEXT, freight REAL DEFAULT 0,
          freight_free INTEGER DEFAULT 0, freight_payer TEXT, sale_total REAL DEFAULT 0,
          type TEXT DEFAULT 'sale', cost_total REAL DEFAULT 0,
          orig_total REAL DEFAULT 0, discount_total REAL DEFAULT 0,
          pay_status TEXT DEFAULT 'unpaid', note TEXT, created_at TEXT,
          status TEXT DEFAULT 'pending',
          voided INTEGER DEFAULT 0, voided_at TEXT);
        CREATE TABLE IF NOT EXISTS outbound_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, wine_id INTEGER,
          batch TEXT DEFAULT '', qty INTEGER, unit_price REAL, orig_price REAL,
          discount_rate REAL, discount_amount REAL DEFAULT 0,
          cost_price REAL DEFAULT 0, reason TEXT,
          confirmed_qty INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS transfers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, transfer_no TEXT UNIQUE NOT NULL,
          from_warehouse_id INTEGER, to_warehouse_id INTEGER, wine_id INTEGER,
          batch TEXT DEFAULT '', qty INTEGER, created_at TEXT, note TEXT,
          status TEXT DEFAULT 'pending',
          voided INTEGER DEFAULT 0, voided_at TEXT);
        CREATE TABLE IF NOT EXISTS order_confirms(
          id INTEGER PRIMARY KEY AUTOINCREMENT, order_type TEXT, order_id INTEGER,
          item_id INTEGER, qty INTEGER, amount REAL DEFAULT 0, profit REAL DEFAULT 0,
          created_at TEXT);
        CREATE TABLE IF NOT EXISTS payments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, order_type TEXT, order_id INTEGER,
          amount REAL, pay_date TEXT, note TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS idempotency_keys(
          key TEXT PRIMARY KEY, order_type TEXT, order_id INTEGER,
          order_no TEXT, created_at TEXT);
        """)
        # 轻量迁移: 老库补列 (自饮/赠送/破损/折扣新增字段, 已有行 type 默认 sale)
        for _t, _col, _ddl in (
                ("inbound_orders", "voided", "INTEGER DEFAULT 0"),
                ("inbound_orders", "voided_at", "TEXT"),
                ("outbound_orders", "voided", "INTEGER DEFAULT 0"),
                ("outbound_orders", "voided_at", "TEXT"),
                ("transfers", "voided", "INTEGER DEFAULT 0"),
                ("transfers", "voided_at", "TEXT"),
                ("outbound_orders", "type", "TEXT DEFAULT 'sale'"),
                ("outbound_orders", "cost_total", "REAL DEFAULT 0"),
                ("outbound_orders", "orig_total", "REAL DEFAULT 0"),
                ("outbound_orders", "discount_total", "REAL DEFAULT 0"),
                ("outbound_items", "orig_price", "REAL"),
                ("outbound_items", "discount_rate", "REAL"),
                ("outbound_items", "discount_amount", "REAL DEFAULT 0"),
                ("outbound_items", "cost_price", "REAL DEFAULT 0"),
                ("outbound_items", "reason", "TEXT"),
                ("inbound_orders", "status", "TEXT DEFAULT 'pending'"),
                ("inbound_items", "confirmed_qty", "INTEGER DEFAULT 0"),
                ("outbound_orders", "status", "TEXT DEFAULT 'pending'"),
                ("outbound_items", "confirmed_qty", "INTEGER DEFAULT 0"),
                ("transfers", "status", "TEXT DEFAULT 'pending'")):
            _cols = [r[1] for r in c.execute(f"PRAGMA table_info({_t})")]
            if _col not in _cols:
                c.execute(f"ALTER TABLE {_t} ADD COLUMN {_col} {_ddl}")
        # 无默认用户: 首位管理员由首次注册产生, 注册后即关闭.
        # 示例数据只在仓库表为空时插一次, 与用户数无关, 避免重复插.
        if c.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0] == 0:
            c.execute("INSERT INTO warehouses(name) VALUES('主仓'),('副仓')")
            c.execute("INSERT INTO suppliers(name,phone,note) VALUES"
                      "('示例供应商','13800000000','示例数据')")
            c.execute("INSERT INTO customers(name,phone,note) VALUES"
                      "('示例客户','13900000000','示例数据')")
            now = datetime.now().isoformat()
            c.execute("INSERT INTO wines(barcode,name,brand,spec,origin,description,"
                      "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                      ("6900000000001", "示例红酒 A", "示例品牌", "750ml*6瓶/箱",
                       "法国", "示例酒，仅用于演示", now, now))
            c.execute("INSERT INTO wines(barcode,name,brand,spec,origin,description,"
                      "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                      ("6900000000002", "示例白酒 B", "示例品牌", "500ml*6瓶/箱",
                       "中国", "示例酒，仅用于演示", now, now))


def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000)
    return f"pbkdf2:{salt}:{dk.hex()}"


def check_pw(pw: str, h: str) -> bool:
    try:
        _, salt, dk = h.split(":")
        return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt),
                                   200_000).hex() == dk
    except Exception:
        return False


def audit(c, username, action, target="", note=""):
    c.execute("INSERT INTO audit_logs(username,action,target,note,created_at)"
              " VALUES(?,?,?,?,?)", (username, action, target, note,
                                     datetime.now().isoformat()))


def rowdict(r):
    return dict(r) if r else None


def rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args)]


# ---------- Auth ----------
async def current_user(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE session_token=? AND active=1",
                      (token,)).fetchone()
        if not r:
            return None
        exp = r["session_expires_at"]
        if exp and datetime.fromisoformat(exp) < datetime.now():
            return None
        return dict(r)


async def require_user(request: Request):
    u = await current_user(request)
    if not u:
        return None, JSONResponse({"detail": "未登录"}, status_code=401)
    if u.get("must_change_password"):
        return None, JSONResponse({"detail": "请先修改默认密码",
                                   "must_change_password": True}, status_code=403)
    return u, None


def err(msg, code=400):
    return JSONResponse({"detail": msg}, status_code=code)


@app.on_event("startup")
def startup():
    init_db()


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/version")
def version_info():
    return {"version": APP_VERSION, "build_date": BUILD_DATE, "commit": _git_commit()}


@app.get("/api/config")
def config_info():
    return {"version": APP_VERSION, "build_date": BUILD_DATE,
            "commit": _git_commit(), "port": int(os.environ.get("PORT", "9000"))}


@app.get("/api/auth/status")
def auth_status():
    with conn() as c:
        n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {"has_users": n > 0, "registration_open": n == 0}


@app.post("/api/register")
async def register_first_admin(request: Request):
    b = await request.json()
    username = (b.get("username") or "").strip()
    password = b.get("password") or ""
    if not username:
        return err("用户名必填")
    if len(password) < 6:
        return err("密码至少 6 位")
    with conn() as c:
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
            return JSONResponse({"detail": "注册已关闭，首位管理员已注册"},
                                status_code=403)
        if c.execute("SELECT 1 FROM users WHERE username=?",
                     (username,)).fetchone():
            return err("用户名已存在")
        token = secrets.token_hex(32)
        exp = (datetime.now() + timedelta(hours=TOKEN_TTL_HOURS)).isoformat()
        c.execute("INSERT INTO users(username,password_hash,"
                  "must_change_password,session_token,session_expires_at,"
                  "created_at) VALUES(?,?,?,?,?,?)",
                  (username, hash_pw(password), 0, token, exp,
                   datetime.now().isoformat()))
        audit(c, username, "注册首位管理员", username, "首位管理员注册，注册已关闭")
        return {"token": token, "username": username,
                "must_change_password": False}


# ---------- 登录 / 用户 ----------
@app.post("/api/login")
async def login(request: Request):
    b = await request.json()
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE username=?",
                      (b.get("username", ""),)).fetchone()
        if not r or not r["active"] or not check_pw(b.get("password", ""),
                                                    r["password_hash"]):
            return JSONResponse({"detail": "用户名或密码错误"}, status_code=401)
        token = secrets.token_hex(32)
        exp = (datetime.now() + timedelta(hours=TOKEN_TTL_HOURS)).isoformat()
        c.execute("UPDATE users SET session_token=?, session_expires_at=? WHERE id=?",
                  (token, exp, r["id"]))
        audit(c, r["username"], "登录", r["username"], "登录成功")
        return {"token": token, "username": r["username"],
                "must_change_password": bool(r["must_change_password"])}


@app.post("/api/change-password")
async def change_password(request: Request):
    u = await current_user(request)
    if not u:
        return JSONResponse({"detail": "未登录"}, status_code=401)
    b = await request.json()
    if not check_pw(b.get("old_password", ""), u["password_hash"]):
        return err("原密码错误")
    new = b.get("new_password", "")
    if len(new) < 6:
        return err("新密码至少 6 位")
    with conn() as c:
        c.execute("UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                  (hash_pw(new), u["id"]))
        audit(c, u["username"], "改密码", u["username"], "修改密码")
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    u = await current_user(request)
    if not u:
        return JSONResponse({"detail": "未登录"}, status_code=401)
    with conn() as c:
        c.execute("UPDATE users SET session_token=NULL WHERE id=?", (u["id"],))
    return {"ok": True}


@app.get("/api/users")
async def list_users(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        return rows(c, "SELECT id,username,active,"
                       "must_change_password,created_at FROM users ORDER BY id")


@app.post("/api/users")
async def create_user(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    if not b.get("username") or not b.get("password"):
        return err("用户名和密码必填")
    with conn() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?",
                     (b["username"],)).fetchone():
            return err("用户名已存在")
        cur = c.execute("INSERT INTO users(username,password_hash,"
                        "must_change_password,created_at) VALUES(?,?,?,?)",
                        (b["username"],
                         hash_pw(b["password"]), 1 if b.get("must_change_password")
                         else 0, datetime.now().isoformat()))
        audit(c, u["username"], "新增管理员", b["username"], "")
        return {"ok": True, "id": cur.lastrowid}


@app.put("/api/users/{uid}")
async def update_user(uid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not r:
            return err("用户不存在", 404)
        if b.get("password"):
            c.execute("UPDATE users SET password_hash=?, must_change_password=? "
                      "WHERE id=?", (hash_pw(b["password"]),
                                     1 if b.get("must_change_password") else 0, uid))
        if "active" in b:
            c.execute("UPDATE users SET active=? WHERE id=?",
                      (1 if b["active"] else 0, uid))
        audit(c, u["username"], "修改管理员", r["username"],
              "active/password 更新")
        return {"ok": True}


@app.get("/api/audit-logs")
async def list_audit(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        return rows(c, "SELECT * FROM audit_logs ORDER BY id DESC LIMIT 200")

# ---------- 酒库 / 仓库 / 往来单位 ----------
@app.get("/api/wines")
async def list_wines(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        ws = rows(c, "SELECT * FROM wines ORDER BY id DESC")
        for w in ws:
            s = c.execute("SELECT COALESCE(SUM(quantity),0) s FROM stock WHERE wine_id=?",
                          (w["id"],)).fetchone()
            w["stock_total"] = s["s"]
        return ws


@app.post("/api/wines")
async def create_wine(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    if not b.get("name"):
        return err("酒名必填")
    now = datetime.now().isoformat()
    with conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO wines(barcode,name,brand,spec,capacity_ml,abv,origin,"
                "manufacturer,description,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (b.get("barcode") or None, b["name"], b.get("brand"),
                 b.get("spec"), b.get("capacity_ml"), b.get("abv"),
                 b.get("origin"), b.get("manufacturer"),
                 b.get("description"), now, now))
        except sqlite3.IntegrityError:
            return err("条码已存在")
        audit(c, u["username"], "新增酒", b["name"], "")
        return {"ok": True, "id": cur.lastrowid}


@app.put("/api/wines/{wid}")
async def update_wine(wid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    fields = ["barcode", "name", "brand", "spec", "capacity_ml", "abv", "origin",
              "manufacturer", "description"]
    with conn() as c:
        sets = ", ".join(f"{f}=?" for f in fields if f in b)
        if not sets:
            return err("无可更新字段")
        try:
            c.execute(f"UPDATE wines SET {sets}, updated_at=? WHERE id=?",
                      [b[f] for f in fields if f in b] +
                      [datetime.now().isoformat(), wid])
        except sqlite3.IntegrityError:
            return err("条码已存在")
        audit(c, u["username"], "修改酒", str(wid), "")
        return {"ok": True}


@app.post("/api/wines/{wid}/image")
async def upload_wine_image(wid: int, request: Request):
    """图片上传用原始 body (避免引入 python-multipart, 依赖只留 3 个).
    前端 fetch 直接把文件当 body 发, 文件名放 X-Filename 头."""
    u, e = await require_user(request)
    if e:
        return e
    ext = Path(request.headers.get("X-Filename", "")).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        return err("只允许 jpg/png/webp")
    data = await request.body()
    if len(data) > 5 * 1024 * 1024:
        return err("单张图片 ≤5MB")
    fn = f"wine_{wid}_{int(datetime.now().timestamp())}{ext}"
    (UPLOAD_DIR / fn).write_bytes(data)
    with conn() as c:
        c.execute("UPDATE wines SET image_path=? WHERE id=?", (f"uploads/{fn}", wid))
    return {"ok": True, "image_path": f"uploads/{fn}"}


@app.get("/api/barcode")
async def barcode_lookup(request: Request, code: str = ""):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        r = c.execute("SELECT * FROM wines WHERE barcode=?", (code,)).fetchone()
        if r:
            return {"found": True, "source": "local", **rowdict(r)}
    url = os.environ.get("BARCODE_API_URL", "")
    key = os.environ.get("BARCODE_API_KEY", "")
    if not url:
        return {"found": False}
    try:
        sep = "&" if "?" in url else "?"
        full = f"{url}{sep}code={code}"
        req = urllib.request.Request(full, headers={"Authorization": f"Bearer {key}"}
                                     if key else {})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        # 兼容常见字段名, 未知结构则视为未查到
        if isinstance(data, dict):
            item = data.get("data") or data
            if isinstance(item, dict) and (item.get("name") or item.get("goodsName")):
                return {"found": True, "source": "api",
                        "name": item.get("name") or item.get("goodsName"),
                        "brand": item.get("brand") or item.get("brandName"),
                        "spec": item.get("spec") or item.get("specification"),
                        "manufacturer": item.get("manufacturer"),
                        "origin": item.get("origin"),
                        "image_url": item.get("image") or item.get("imageUrl")}
        return {"found": False}
    except Exception:
        return {"found": False}


@app.get("/api/warehouses")
async def list_wh(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        whs = rows(c, "SELECT * FROM warehouses ORDER BY id")
        for w in whs:
            w["stock_total"] = c.execute(
                "SELECT COALESCE(SUM(quantity),0) s FROM stock"
                " WHERE warehouse_id=?", (w["id"],)).fetchone()["s"]
        return whs


@app.post("/api/warehouses")
async def create_wh(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    if not (b.get("name") or "").strip():
        return err("仓库名称必填")
    with conn() as c:
        cur = c.execute("INSERT INTO warehouses(name) VALUES(?)",
                        (b["name"].strip(),))
        audit(c, u["username"], "新增仓库", b["name"].strip(), "")
        return {"ok": True, "id": cur.lastrowid}


@app.put("/api/warehouses/{wid}")
async def update_wh(wid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    if not (b.get("name") or "").strip():
        return err("仓库名称必填")
    with conn() as c:
        r = c.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone()
        if not r:
            return err("仓库不存在", 404)
        c.execute("UPDATE warehouses SET name=? WHERE id=?",
                  (b["name"].strip(), wid))
        audit(c, u["username"], "改名仓库", b["name"].strip(), "")
        return {"ok": True}


@app.delete("/api/warehouses/{wid}")
async def delete_wh(wid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        r = c.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone()
        if not r:
            return err("仓库不存在", 404)
        n = c.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0]
        if n <= 1:
            return err("不能删除最后一个仓库", 400)
        stock = c.execute("SELECT COALESCE(SUM(quantity),0) s FROM stock"
                          " WHERE warehouse_id=?", (wid,)).fetchone()["s"]
        if stock > 0:
            return err(f"该仓库还有库存 {stock} 瓶，不能删除", 400)
        pend = c.execute("SELECT COUNT(*) FROM inbound_orders"
                         " WHERE warehouse_id=? AND type='purchase'"
                         " AND status IN ('pending','partial')",
                         (wid,)).fetchone()[0]
        pend += c.execute("SELECT COUNT(*) FROM outbound_orders"
                          " WHERE warehouse_id=? AND type='sale'"
                          " AND status IN ('pending','partial')",
                          (wid,)).fetchone()[0]
        pend += c.execute("SELECT COUNT(*) FROM transfers"
                          " WHERE (from_warehouse_id=? OR to_warehouse_id=?)"
                          " AND status='pending'", (wid, wid)).fetchone()[0]
        if pend > 0:
            return err(f"该仓库还有 {pend} 张未确认的单据，不能删除", 400)
        c.execute("DELETE FROM warehouses WHERE id=?", (wid,))
        audit(c, u["username"], "删除仓库", r["name"], "")
        return {"ok": True}


@app.get("/api/inventory")
async def inventory(request: Request):
    # 分仓库库存: 每组列酒名、规格、库存瓶数、成本小计, 只读查看
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        out = []
        for w in rows(c, "SELECT * FROM warehouses ORDER BY id"):
            items = rows(c, "SELECT s.wine_id, w.name AS wine_name, w.spec,"
                         " s.quantity AS qty FROM stock s"
                         " JOIN wines w ON w.id=s.wine_id"
                         " WHERE s.warehouse_id=? AND s.quantity>0"
                         " ORDER BY w.name", (w["id"],))
            tot_qty = 0
            tot_cost = 0.0
            for it in items:
                unit = latest_cost(c, it["wine_id"])
                it["unit_cost"] = unit
                it["cost"] = round(unit * it["qty"], 2)
                tot_qty += it["qty"]
                tot_cost += it["cost"]
            out.append({"id": w["id"], "name": w["name"],
                        "total_qty": tot_qty, "total_cost": round(tot_cost, 2),
                        "items": items})
        return {"warehouses": out}


def party_routes(kind, table):
    async def lst(request: Request):
        u, e = await require_user(request)
        if e:
            return e
        with conn() as c:
            ps = rows(c, f"SELECT * FROM {table} ORDER BY id DESC")
            for p in ps:
                if kind == "supplier":
                    t = c.execute("SELECT COALESCE(SUM(i.confirmed_qty*i.unit_price),0)"
                                  " tot FROM inbound_items i JOIN inbound_orders o"
                                  " ON o.id=i.order_id WHERE o.supplier_id=?"
                                  " AND o.type='purchase' AND COALESCE(o.voided,0)=0",
                                  (p["id"],)).fetchone()["tot"]
                    paid = c.execute("SELECT COALESCE(SUM(p.amount),0) s FROM payments p "
                                     "JOIN inbound_orders o ON o.id=p.order_id "
                                     "WHERE p.order_type='inbound' AND o.supplier_id=? "
                                     "AND o.type='purchase' AND COALESCE(o.voided,0)=0",
                                     (p["id"],)).fetchone()["s"]
                else:
                    t = c.execute("SELECT COALESCE(SUM(i.confirmed_qty*i.unit_price),0)"
                                  " tot FROM outbound_items i JOIN outbound_orders o"
                                  " ON o.id=i.order_id WHERE o.customer_id=?"
                                  " AND o.type='sale' AND COALESCE(o.voided,0)=0",
                                  (p["id"],)).fetchone()["tot"]
                    paid = c.execute("SELECT COALESCE(SUM(p.amount),0) s FROM payments p "
                                     "JOIN outbound_orders o ON o.id=p.order_id "
                                     "WHERE p.order_type='outbound' AND o.customer_id=?"
                                     " AND COALESCE(o.voided,0)=0",
                                     (p["id"],)).fetchone()["s"]
                p["balance"] = round(t - paid, 2)
                if kind == "customer":
                    g = c.execute("SELECT COALESCE(SUM(i.qty),0) q,"
                                  " COALESCE(SUM(i.qty*i.cost_price),0) c"
                                  " FROM outbound_items i JOIN outbound_orders o"
                                  " ON o.id=i.order_id"
                                  " WHERE o.type='gift' AND o.customer_id=?"
                                  " AND COALESCE(o.voided,0)=0",
                                  (p["id"],)).fetchone()
                    p["gift_qty"] = int(g["q"])
                    p["gift_cost"] = round(g["c"], 2)
            return ps

    async def create(request: Request):
        u, e = await require_user(request)
        if e:
            return e
        b = await request.json()
        if not b.get("name"):
            return err("名称必填")
        with conn() as c:
            cur = c.execute(f"INSERT INTO {table}(name,phone,address,note)"
                            " VALUES(?,?,?,?)",
                            (b["name"], b.get("phone"),
                             b.get("address"), b.get("note")))
            audit(c, u["username"], f"新增{'供应商' if kind == 'supplier' else '客户'}",
                  b["name"], "")
            return {"ok": True, "id": cur.lastrowid}

    async def update(pid: int, request: Request):
        u, e = await require_user(request)
        if e:
            return e
        b = await request.json()
        fields = ["name", "phone", "address", "note", "active"]
        sets = ", ".join(f"{f}=?" for f in fields if f in b)
        if not sets:
            return err("无可更新字段")
        with conn() as c:
            c.execute(f"UPDATE {table} SET {sets} WHERE id=?",
                      [b[f] for f in fields if f in b] + [pid])
            audit(c, u["username"], f"修改{'供应商' if kind == 'supplier' else '客户'}",
                  str(pid), "")
            return {"ok": True}
    return lst, create, update


_sup_lst, _sup_create, _sup_update = party_routes("supplier", "suppliers")
_cus_lst, _cus_create, _cus_update = party_routes("customer", "customers")
app.get("/api/suppliers")(_sup_lst)
app.post("/api/suppliers")(_sup_create)
app.put("/api/suppliers/{pid}")(_sup_update)
app.get("/api/customers")(_cus_lst)
app.post("/api/customers")(_cus_create)
app.put("/api/customers/{pid}")(_cus_update)


@app.delete("/api/suppliers/{pid}")
async def delete_supplier(pid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        r = c.execute("SELECT * FROM suppliers WHERE id=?", (pid,)).fetchone()
        if not r:
            return err("供应商不存在", 404)
        n = c.execute("SELECT COUNT(*) FROM inbound_orders WHERE supplier_id=? AND type='purchase' AND COALESCE(voided,0)=0", (pid,)).fetchone()[0]
        if n > 0:
            return JSONResponse({"detail": "该供应商已有进货单，不能删除"}, status_code=400)
        c.execute("DELETE FROM suppliers WHERE id=?", (pid,))
        audit(c, u["username"], "删除供应商", r["name"], "")
        return {"ok": True}


@app.delete("/api/customers/{pid}")
async def delete_customer(pid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        r = c.execute("SELECT * FROM customers WHERE id=?", (pid,)).fetchone()
        if not r:
            return err("客户不存在", 404)
        n = c.execute("SELECT COUNT(*) FROM outbound_orders WHERE customer_id=? AND COALESCE(voided,0)=0", (pid,)).fetchone()[0]
        if n > 0:
            return JSONResponse({"detail": "该客户已有出货单，不能删除"}, status_code=400)
        c.execute("DELETE FROM customers WHERE id=?", (pid,))
        audit(c, u["username"], "删除客户", r["name"], "")
        return {"ok": True}


# ---------- 库存 / 单据 ----------
def next_order_no(c, table, prefix, col="order_no"):
    today = date.today().strftime("%Y%m%d")
    n = c.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?",
                  (f"{prefix}{today}-%",)).fetchone()[0] + 1
    return f"{prefix}{today}-{n:03d}"


def add_stock(c, wh, wine, batch, qty):
    c.execute("INSERT INTO stock(warehouse_id,wine_id,batch,quantity) VALUES(?,?,?,?)"
              " ON CONFLICT(warehouse_id,wine_id,batch) DO UPDATE SET"
              " quantity=quantity+excluded.quantity",
              (wh, wine, batch or "", qty))


def get_stock(c, wh, wine, batch=""):
    r = c.execute("SELECT quantity FROM stock WHERE warehouse_id=? AND wine_id=?"
                  " AND batch=?", (wh, wine, batch or "")).fetchone()
    return r["quantity"] if r else 0


def paid_total(c, otype, oid):
    # 已作废的单: 已付同步冲回, 按 0 算
    table = "inbound_orders" if otype == "inbound" else "outbound_orders"
    o = c.execute(f"SELECT voided FROM {table} WHERE id=?", (oid,)).fetchone()
    if o and o["voided"]:
        return 0.0
    r = c.execute("SELECT COALESCE(SUM(amount),0) s FROM payments WHERE order_type=?"
                  " AND order_id=?", (otype, oid)).fetchone()
    return r["s"]


def idem_lookup(c, key):
    """幂等键查原单: 有则返回键记录, 无则返回 None."""
    if not key:
        return None
    return c.execute("SELECT * FROM idempotency_keys WHERE key=?",
                     (key,)).fetchone()


def idem_response(c, row):
    """按原单重建创建成功时的返回体, 不重复建单."""
    otype, oid = row["order_type"], row["order_id"]
    if otype == "transfer":
        tr = c.execute("SELECT * FROM transfers WHERE id=?", (oid,)).fetchone()
        return {"ok": True, "transfer_no": tr["transfer_no"], "duplicate": True}
    if otype == "inbound":
        o = c.execute("SELECT * FROM inbound_orders WHERE id=?", (oid,)).fetchone()
        return {"ok": True, "id": oid, "order_no": o["order_no"],
                "goods_total": o["goods_total"], "freight": o["freight"],
                "duplicate": True}
    o = c.execute("SELECT * FROM outbound_orders WHERE id=?", (oid,)).fetchone()
    return {"ok": True, "id": oid, "order_no": o["order_no"], "type": o["type"],
            "sale_total": o["sale_total"], "orig_total": o["orig_total"],
            "discount_total": o["discount_total"], "cost_total": o["cost_total"],
            "pay_status": o["pay_status"], "freight": o["freight"],
            "duplicate": True}


def idem_store(c, key, otype, oid, order_no):
    """建单成功后记下幂等键, 同一键再来直接返回原单."""
    if key:
        c.execute("INSERT OR IGNORE INTO idempotency_keys"
                  "(key,order_type,order_id,order_no,created_at)"
                  " VALUES(?,?,?,?,?)",
                  (key, otype, oid, order_no, datetime.now().isoformat()))


def pay_status(paid, total):
    if paid <= 0:
        return "unpaid"
    if paid + 1e-9 >= total:
        return "paid"
    return "partial"


def confirmed_total(c, table, items_table, oid):
    """已确认货值: 明细 confirmed_qty * 单价之和 (未确认的不算)."""
    r = c.execute(f"SELECT COALESCE(SUM(confirmed_qty*unit_price),0) s"
                  f" FROM {items_table} WHERE order_id=?", (oid,)).fetchone()
    return r["s"]


def pay_status_confirmed(c, table, items_table, oid, paid):
    """按已确认货值算付款状态: 没确认过时已付算预付, 显示 partial."""
    tot = confirmed_total(c, table, items_table, oid)
    if tot <= 0:
        return "unpaid" if paid <= 0 else "partial"
    return pay_status(paid, tot)


def order_status_after(c, table, items_table, oid, final_status):
    """按明细确认进度推导单据状态: 全确认 -> final_status, 部分 -> partial, 无 -> pending."""
    r = c.execute(f"SELECT COALESCE(SUM(qty),0) q,"
                  f" COALESCE(SUM(confirmed_qty),0) cq FROM {items_table}"
                  f" WHERE order_id=?", (oid,)).fetchone()
    if r["cq"] <= 0:
        return "pending"
    if r["cq"] + 1e-9 >= r["q"]:
        return final_status
    return "partial"


def refresh_order_pay_status(c, table, items_table, oid):
    """确认/废单后同步 pay_status (按已确认货值算)."""
    o = c.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
    if not o:
        return
    if table == "outbound_orders" and o["type"] != "sale":
        return
    paid = paid_total(c, "inbound" if table == "inbound_orders" else "outbound",
                      oid)
    c.execute(f"UPDATE {table} SET pay_status=? WHERE id=?",
              (pay_status_confirmed(c, table, items_table, oid, paid), oid))


def latest_cost(c, wine_id):
    r = c.execute("SELECT unit_price FROM inbound_items WHERE wine_id=? ORDER BY id DESC"
                  " LIMIT 1", (wine_id,)).fetchone()
    return r["unit_price"] if r else 0.0


def order_detail(c, table, items_table, oid):
    o = c.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
    if not o:
        return None
    d = dict(o)
    d["items"] = rows(c, f"SELECT i.*, w.name AS wine_name, w.spec FROM {items_table} i"
                         " JOIN wines w ON w.id=i.wine_id WHERE i.order_id=?", (oid,))
    otype = "inbound" if table == "inbound_orders" else "outbound"
    d["paid_amount"] = paid_total(c, otype, oid)
    return d

@app.get("/api/dashboard")
async def dashboard(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    today = date.today().isoformat()
    month = date.today().strftime("%Y-%m")
    with conn() as c:
        # 今日销售额/毛利: 只算今天确认的销售单已确认部分 (确认时间, 不是下单时间)
        sales = c.execute("SELECT COALESCE(SUM(oc.amount),0) s FROM order_confirms oc"
                          " JOIN outbound_orders o ON o.id=oc.order_id"
                          " WHERE oc.order_type='outbound' AND o.type='sale'"
                          " AND substr(oc.created_at,1,10)=?", (today,)).fetchone()["s"]
        profit = c.execute("SELECT COALESCE(SUM(oc.profit),0) s FROM order_confirms oc"
                           " JOIN outbound_orders o ON o.id=oc.order_id"
                           " WHERE oc.order_type='outbound' AND o.type='sale'"
                           " AND substr(oc.created_at,1,10)=?",
                           (today,)).fetchone()["s"]
        # 欠款: 只算已确认货值 - 已付 (未确认的单不计入)
        cust_debt = 0.0
        for o in rows(c, "SELECT id FROM outbound_orders"
                         " WHERE type='sale' AND COALESCE(voided,0)=0"):
            cust_debt += (confirmed_total(c, "outbound_orders", "outbound_items",
                                          o["id"])
                          - paid_total(c, "outbound", o["id"]))
        sup_debt = 0.0
        for o in rows(c, "SELECT id FROM inbound_orders"
                         " WHERE type='purchase' AND COALESCE(voided,0)=0"):
            sup_debt += (confirmed_total(c, "inbound_orders", "inbound_items",
                                         o["id"])
                         - paid_total(c, "inbound", o["id"]))
        low = rows(c, "SELECT w.id, w.name, w.spec, COALESCE(SUM(s.quantity),0) qty"
                      " FROM wines w LEFT JOIN stock s ON s.wine_id=w.id"
                      " GROUP BY w.id HAVING qty < ?", (LOW_STOCK_THRESHOLD,))

        def month_stat(typ):
            r = c.execute("SELECT COALESCE(SUM(i.qty),0) q,"
                          " COALESCE(SUM(i.qty*i.cost_price),0) c FROM outbound_items i"
                          " JOIN outbound_orders o ON o.id=i.order_id"
                          " WHERE o.type=? AND substr(o.order_date,1,7)=?"
                          " AND COALESCE(o.voided,0)=0",
                          (typ, month)).fetchone()
            return int(r["q"]), round(r["c"], 2)
        pq, pc = month_stat("personal")
        gq, gc = month_stat("gift")
        dq, dc = month_stat("damage")
        month_discount = c.execute(
            "SELECT COALESCE(SUM((i.orig_price-i.unit_price)*i.confirmed_qty),0)"
            " FROM outbound_items i JOIN outbound_orders o ON o.id=i.order_id"
            " WHERE o.type='sale' AND substr(o.order_date,1,7)=?"
            " AND COALESCE(o.voided,0)=0", (month,)).fetchone()[0]
        return {"today_sales": round(sales, 2), "today_profit": round(profit, 2),
                "customer_debt": round(cust_debt, 2),
                "supplier_debt": round(sup_debt, 2),
                "low_stock": low, "low_threshold": LOW_STOCK_THRESHOLD,
                "month_personal_qty": pq, "month_personal_cost": pc,
                "month_gift_qty": gq, "month_gift_cost": gc,
                "month_damage_qty": dq, "month_damage_cost": dc,
                "month_discount_total": round(month_discount, 2)}


@app.post("/api/inbound")
async def create_inbound(request: Request):
    """type=purchase 新进货 / opening 补登期初."""
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    typ = b.get("type", "purchase")
    if typ not in ("purchase", "opening"):
        return err("type 只能是 purchase/opening")
    items = b.get("items") or []
    if not items:
        return err("至少一行商品")
    freight = 0.0 if b.get("freight_free") else float(b.get("freight") or 0)
    goods = sum(float(i.get("qty", 0)) * float(i.get("unit_price") or 0)
                for i in items)
    with conn() as c:
        _key = (b.get("idempotency_key") or "").strip() or None
        _dup = idem_lookup(c, _key)
        if _dup is not None:
            return idem_response(c, _dup)
        no = next_order_no(c, "inbound_orders", "BD" if typ == "opening" else "RK")
        # 两段式: purchase 下单时状态待到货, 不加库存、不算欠款, 确认到货才动;
        # opening 补登是库存调整, 立即生效
        status = "received" if typ == "opening" else "pending"
        cur = c.execute(
            "INSERT INTO inbound_orders(order_no,type,supplier_id,warehouse_id,"
            "order_date,carrier,tracking_no,freight,freight_free,freight_payer,"
            "goods_total,pay_status,note,created_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (no, typ, b.get("supplier_id"), b.get("warehouse_id"),
             b.get("order_date") or date.today().isoformat(), b.get("carrier"),
             b.get("tracking_no"), freight, 1 if b.get("freight_free") else 0,
             b.get("freight_payer"), round(goods, 2), "unpaid", b.get("note"),
             datetime.now().isoformat(), status))
        oid = cur.lastrowid
        for i in items:
            c.execute("INSERT INTO inbound_items(order_id,wine_id,batch,qty,unit_price,"
                      "confirmed_qty) VALUES(?,?,?,?,?,?)",
                      (oid, i["wine_id"], i.get("batch") or "",
                       int(i["qty"]), float(i.get("unit_price") or 0),
                       int(i["qty"]) if typ == "opening" else 0))
            if typ == "opening":
                add_stock(c, b["warehouse_id"], i["wine_id"], i.get("batch") or "",
                          int(i["qty"]))
        paid = float(b.get("paid_amount") or 0)
        if paid > 0:
            c.execute("INSERT INTO payments(order_type,order_id,amount,pay_date,note,"
                      "created_at) VALUES('inbound',?,?,?,?,?)",
                      (oid, paid, date.today().isoformat(), "建单时已付",
                       datetime.now().isoformat()))
        c.execute("UPDATE inbound_orders SET pay_status=? WHERE id=?",
                  (pay_status_confirmed(c, "inbound_orders", "inbound_items", oid,
                                        paid), oid))
        audit(c, u["username"], "补登期初" if typ == "opening" else "进货",
              no, f"货款{goods:.2f} 运费{freight:.2f}")
        idem_store(c, _key, "inbound", oid, no)
        return {"ok": True, "id": oid, "order_no": no, "goods_total": round(goods, 2),
                "freight": freight, "status": status}


@app.get("/api/inbound")
async def list_inbound(request: Request, type: str = "", supplier_id: str = "",
                       date_from: str = "", date_to: str = ""):
    u, e = await require_user(request)
    if e:
        return e
    q, args = "SELECT * FROM inbound_orders WHERE 1=1", []
    if type:
        q += " AND type=?"; args.append(type)
    if supplier_id:
        q += " AND supplier_id=?"; args.append(supplier_id)
    if date_from:
        q += " AND order_date>=?"; args.append(date_from)
    if date_to:
        q += " AND order_date<=?"; args.append(date_to)
    with conn() as c:
        ds = rows(c, q + " ORDER BY id DESC", args)
        for d in ds:
            d["paid_amount"] = paid_total(c, "inbound", d["id"])
        return ds


@app.get("/api/inbound/{oid}")
async def get_inbound(oid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        d = order_detail(c, "inbound_orders", "inbound_items", oid)
        return d or err("单据不存在", 404)


@app.post("/api/outbound")
async def create_outbound(request: Request):
    """type=sale 销售出库 / personal 自饮 / gift 赠送 / damage 破损.
    非销售: 不算收入、不产生应收, 按成本价记成本. 折扣只针对销售出库."""
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    typ = b.get("type", "sale")
    if typ not in ("sale", "personal", "gift", "damage"):
        return err("type 只能是 sale/personal/gift/damage")
    items = b.get("items") or []
    if not items:
        return err("至少一行商品")
    if typ == "gift" and not b.get("customer_id"):
        return err("赠送必须选择受赠客户")
    if typ in ("personal", "damage") and b.get("customer_id"):
        return err("自饮/破损不需要客户")
    wh = b.get("warehouse_id")
    with conn() as c:
        _key = (b.get("idempotency_key") or "").strip() or None
        _dup = idem_lookup(c, _key)
        if _dup is not None:
            return idem_response(c, _dup)
        for i in items:  # 先校验全部, 不足拦住, 不允许负库存
            have = get_stock(c, wh, i["wine_id"], i.get("batch") or "")
            if int(i["qty"]) > have:
                w = c.execute("SELECT name FROM wines WHERE id=?",
                              (i["wine_id"],)).fetchone()
                return err(f"库存不足: {w['name'] if w else i['wine_id']} "
                           f"现有 {have} 瓶, 要出 {i['qty']} 瓶")
        # 逐行算价: 销售可用折扣 (折扣率或折后单价二选一); 非销售按成本价记成本
        lines = []
        for i in items:
            qty = int(i["qty"])
            cost = latest_cost(c, i["wine_id"])
            orig = float(i.get("orig_price") or i.get("unit_price") or 0)
            rate = i.get("discount_rate")
            dprice = i.get("discount_price")
            if rate in ("", None):
                rate = None
            if dprice in ("", None):
                dprice = None
            reason = (i.get("reason") or "").strip()
            if typ == "sale":
                if rate is not None or dprice is not None:
                    if not reason and not (b.get("note") or "").strip():
                        return err("打折需填写瑕疵/打折原因")
                    if rate is not None:
                        rate = float(rate)
                        if not 0 < rate <= 1:
                            return err("折扣率须在 0 到 1 之间 (如 0.9 为 9 折)")
                        final = round(orig * rate, 2)
                    else:
                        final = float(dprice)
                        if final < 0 or final > orig:
                            return err("折后单价须在 0 到原价之间")
                        rate = round(final / orig, 4) if orig else None
                    disc = round((orig - final) * qty, 2)
                else:
                    rate, final, disc = None, orig, 0.0
                lines.append({"wine_id": i["wine_id"], "batch": i.get("batch") or "",
                              "qty": qty, "unit_price": final, "orig_price": orig,
                              "discount_rate": rate, "discount_amount": disc,
                              "cost_price": cost, "reason": reason or None})
            else:
                lines.append({"wine_id": i["wine_id"], "batch": i.get("batch") or "",
                              "qty": qty, "unit_price": 0.0, "orig_price": 0.0,
                              "discount_rate": None, "discount_amount": 0.0,
                              "cost_price": cost, "reason": reason or None})
        if typ == "sale":
            freight = 0.0 if b.get("freight_free") else float(b.get("freight") or 0)
            total = round(sum(l["qty"] * l["unit_price"] for l in lines), 2)
            orig_total = round(sum(l["qty"] * l["orig_price"] for l in lines), 2)
            disc_total = round(sum(l["discount_amount"] for l in lines), 2)
        else:
            freight, total, orig_total, disc_total = 0.0, 0.0, 0.0, 0.0
        cost_total = round(sum(l["qty"] * l["cost_price"] for l in lines), 2)
        no = next_order_no(c, "outbound_orders", "CK")
        # 两段式: sale 下单时状态待出货, 不减库存、不算欠款, 确认出货才动;
        # 自饮/赠送/破损立即生效
        status = "done" if typ != "sale" else "pending"
        cur = c.execute(
            "INSERT INTO outbound_orders(order_no,type,customer_id,warehouse_id,"
            "order_date,carrier,tracking_no,freight,freight_free,freight_payer,"
            "sale_total,orig_total,discount_total,cost_total,pay_status,note,created_at,"
            "status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (no, typ, b.get("customer_id") if typ in ("sale", "gift") else None,
             wh, b.get("order_date") or date.today().isoformat(),
             b.get("carrier"), b.get("tracking_no"), freight,
             1 if b.get("freight_free") else 0, b.get("freight_payer"),
             total, orig_total, disc_total, cost_total,
             "unpaid" if typ == "sale" else "paid",
             b.get("note"), datetime.now().isoformat(), status))
        oid = cur.lastrowid
        for l in lines:
            c.execute("INSERT INTO outbound_items(order_id,wine_id,batch,qty,unit_price,"
                      "orig_price,discount_rate,discount_amount,cost_price,reason,"
                      "confirmed_qty) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (oid, l["wine_id"], l["batch"], l["qty"], l["unit_price"],
                       l["orig_price"], l["discount_rate"], l["discount_amount"],
                       l["cost_price"], l["reason"],
                       l["qty"] if typ != "sale" else 0))
            if typ != "sale":
                add_stock(c, wh, l["wine_id"], l["batch"], -l["qty"])
        paid = float(b.get("paid_amount") or 0) if typ == "sale" else 0.0
        if paid > 0:
            c.execute("INSERT INTO payments(order_type,order_id,amount,pay_date,note,"
                      "created_at) VALUES('outbound',?,?,?,?,?)",
                      (oid, paid, date.today().isoformat(), "建单时已收",
                       datetime.now().isoformat()))
        if typ == "sale":
            c.execute("UPDATE outbound_orders SET pay_status=? WHERE id=?",
                      (pay_status_confirmed(c, "outbound_orders", "outbound_items",
                                            oid, paid), oid))
        action = {"sale": "出货", "personal": "自饮", "gift": "赠送",
                  "damage": "破损"}[typ]
        note = (f"货款{total:.2f} 运费{freight:.2f}" if typ == "sale"
                else f"成本{cost_total:.2f} {sum(l['qty'] for l in lines)} 瓶")
        if disc_total:
            note += f" 折扣{disc_total:.2f}"
        audit(c, u["username"], action, no, note)
        idem_store(c, _key, "outbound", oid, no)
        return {"ok": True, "id": oid, "order_no": no, "type": typ,
                "sale_total": total, "orig_total": orig_total,
                "discount_total": disc_total, "cost_total": cost_total,
                "pay_status": pay_status_confirmed(
                    c, "outbound_orders", "outbound_items", oid, paid)
                if typ == "sale" else "paid",
                "freight": freight, "status": status}


@app.get("/api/outbound")
async def list_outbound(request: Request, customer_id: str = "",
                        type: str = "", date_from: str = "", date_to: str = ""):
    u, e = await require_user(request)
    if e:
        return e
    q, args = "SELECT * FROM outbound_orders WHERE 1=1", []
    if customer_id:
        q += " AND customer_id=?"; args.append(customer_id)
    if type:
        q += " AND type=?"; args.append(type)
    if date_from:
        q += " AND order_date>=?"; args.append(date_from)
    if date_to:
        q += " AND order_date<=?"; args.append(date_to)
    with conn() as c:
        ds = rows(c, q + " ORDER BY id DESC", args)
        for d in ds:
            d["paid_amount"] = paid_total(c, "outbound", d["id"])
        return ds


@app.get("/api/outbound/{oid}")
async def get_outbound(oid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        d = order_detail(c, "outbound_orders", "outbound_items", oid)
        return d or err("单据不存在", 404)


@app.get("/api/customers/{cid}/gifts")
async def customer_gifts(cid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        cus = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        if not cus:
            return err("客户不存在", 404)
        tot = c.execute("SELECT COALESCE(SUM(i.qty),0) q,"
                        " COALESCE(SUM(i.qty*i.cost_price),0) c FROM outbound_items i"
                        " JOIN outbound_orders o ON o.id=i.order_id"
                        " WHERE o.type='gift' AND o.customer_id=?"
                        " AND COALESCE(o.voided,0)=0", (cid,)).fetchone()
        by_wine = rows(c, "SELECT w.id AS wine_id, w.name AS wine_name,"
                          " COALESCE(SUM(i.qty),0) qty,"
                          " COALESCE(SUM(i.qty*i.cost_price),0) cost"
                          " FROM outbound_items i JOIN outbound_orders o ON o.id=i.order_id"
                          " JOIN wines w ON w.id=i.wine_id"
                          " WHERE o.type='gift' AND o.customer_id=?"
                          " AND COALESCE(o.voided,0)=0 GROUP BY w.id", (cid,))
        orders = rows(c, "SELECT o.*, COALESCE(SUM(i.qty),0) qty FROM outbound_orders o"
                         " LEFT JOIN outbound_items i ON i.order_id=o.id"
                         " WHERE o.type='gift' AND o.customer_id=?"
                         " AND COALESCE(o.voided,0)=0"
                         " GROUP BY o.id ORDER BY o.id DESC", (cid,))
        return {"customer": dict(cus), "total_qty": int(tot["q"]),
                "total_cost": round(tot["c"], 2), "by_wine": by_wine, "orders": orders}


@app.post("/api/orders/{otype}/{oid}/payments")
async def add_payment(otype: str, oid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    if otype not in ("inbound", "outbound"):
        return err("otype 只能是 inbound/outbound")
    b = await request.json()
    amt = float(b.get("amount") or 0)
    if amt <= 0:
        return err("金额必须大于 0")
    table = "inbound_orders" if otype == "inbound" else "outbound_orders"
    total_field = "goods_total" if otype == "inbound" else "sale_total"
    with conn() as c:
        o = c.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
        if not o:
            return err("单据不存在", 404)
        if o["voided"]:
            return err("已作废的单据不能补记", 400)
        if otype == "outbound" and o["type"] != "sale":
            return err("自饮/赠送/破损单不产生应收, 不能补记收款")
        c.execute("INSERT INTO payments(order_type,order_id,amount,pay_date,note,"
                  "created_at) VALUES(?,?,?,?,?,?)",
                  (otype, oid, amt, b.get("pay_date") or date.today().isoformat(),
                   b.get("note"), datetime.now().isoformat()))
        paid = paid_total(c, otype, oid)
        items_table = "inbound_items" if otype == "inbound" else "outbound_items"
        st = pay_status_confirmed(c, table, items_table, oid, paid)
        c.execute(f"UPDATE {table} SET pay_status=? WHERE id=?", (st, oid))
        audit(c, u["username"], "收款" if otype == "outbound" else "付款",
              o["order_no"], f"补记 {amt:.2f}")
        return {"ok": True, "paid_amount": paid, "pay_status": st}


@app.post("/api/transfers")
async def create_transfer(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    qty = int(b.get("qty") or 0)
    if qty <= 0:
        return err("数量必须大于 0")
    if b.get("from_warehouse_id") == b.get("to_warehouse_id"):
        return err("调出和调入仓库不能相同")
    with conn() as c:
        _key = (b.get("idempotency_key") or "").strip() or None
        _dup = idem_lookup(c, _key)
        if _dup is not None:
            return idem_response(c, _dup)
        have = get_stock(c, b["from_warehouse_id"], b["wine_id"], b.get("batch") or "")
        if qty > have:
            return err(f"源仓库存不足, 现有 {have} 瓶")
        no = next_order_no(c, "transfers", "DB", col="transfer_no")
        cur = c.execute("INSERT INTO transfers(transfer_no,from_warehouse_id,"
                        "to_warehouse_id,wine_id,batch,qty,created_at,note,status)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (no, b["from_warehouse_id"], b["to_warehouse_id"], b["wine_id"],
                         b.get("batch") or "", qty, datetime.now().isoformat(),
                         b.get("note"), "pending"))
        tid = cur.lastrowid
        audit(c, u["username"], "调拨下单", no, f"{qty} 瓶 (待调拨)")
        idem_store(c, _key, "transfer", tid, no)
        return {"ok": True, "transfer_no": no, "status": "pending"}


@app.get("/api/transfers")
async def list_transfers(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        return rows(c, "SELECT t.*, w.name AS wine_name,"
                       " wf.name AS from_warehouse_name, wt.name AS to_warehouse_name"
                       " FROM transfers t"
                       " JOIN wines w ON w.id=t.wine_id"
                       " LEFT JOIN warehouses wf ON wf.id=t.from_warehouse_id"
                       " LEFT JOIN warehouses wt ON wt.id=t.to_warehouse_id"
                       " ORDER BY t.id DESC")


@app.post("/api/inbound/{oid}/confirm")
async def confirm_inbound(oid: int, request: Request):
    """确认到货: 按行确认数量 (默认剩余全部), 确认多少加多少库存、算多少欠款."""
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    with conn() as c:
        o = c.execute("SELECT * FROM inbound_orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return err("单据不存在", 404)
        if o["type"] == "opening":
            return err("补登单无需确认到货", 400)
        if o["voided"]:
            return err("已废单，不能确认", 400)
        if o["status"] in ("received", "closed"):
            return err("该单已确认完或已关闭，不能再确认", 400)
        items = rows(c, "SELECT * FROM inbound_items WHERE order_id=?", (oid,))
        by_id = {it["id"]: it for it in items}
        req_items = b.get("items")
        if req_items is None:
            req_items = [{"item_id": it["id"],
                          "qty": it["qty"] - it["confirmed_qty"]} for it in items]
        now = datetime.now().isoformat()
        total_qty = 0
        total_amt = 0.0
        for ri in req_items:
            it = by_id.get(int(ri.get("item_id") or 0))
            if not it:
                return err("明细不存在", 400)
            q = int(ri.get("qty") or 0)
            remain = it["qty"] - it["confirmed_qty"]
            if q <= 0:
                return err("确认数量必须大于 0", 400)
            if q > remain:
                return err(f"{it['wine_id']} 号酒确认数量超过剩余未到货数量 ({remain} 瓶)",
                           400)
            c.execute("UPDATE inbound_items SET confirmed_qty=confirmed_qty+?"
                      " WHERE id=?", (q, it["id"]))
            add_stock(c, o["warehouse_id"], it["wine_id"], it["batch"] or "", q)
            amt = round(q * it["unit_price"], 2)
            c.execute("INSERT INTO order_confirms(order_type,order_id,item_id,qty,"
                      "amount,profit,created_at) VALUES('inbound',?,?,?,?,?,?)",
                      (oid, it["id"], q, amt, 0.0, now))
            total_qty += q
            total_amt += amt
        st = order_status_after(c, "inbound_orders", "inbound_items", oid, "received")
        c.execute("UPDATE inbound_orders SET status=? WHERE id=?", (st, oid))
        refresh_order_pay_status(c, "inbound_orders", "inbound_items", oid)
        audit(c, u["username"], "确认到货", o["order_no"],
              f"确认 {total_qty} 瓶, 货值 {total_amt:.2f}")
        return {"ok": True, "order_no": o["order_no"], "status": st,
                "confirmed_qty": total_qty, "confirmed_amount": round(total_amt, 2)}


@app.post("/api/outbound/{oid}/confirm")
async def confirm_outbound(oid: int, request: Request):
    """确认出货: 按行确认数量 (默认剩余全部), 确认多少减多少库存、算多少欠款."""
    u, e = await require_user(request)
    if e:
        return e
    b = await request.json()
    with conn() as c:
        o = c.execute("SELECT * FROM outbound_orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return err("单据不存在", 404)
        if o["type"] != "sale":
            return err("自饮/赠送/破损单建单即生效，无需确认", 400)
        if o["voided"]:
            return err("已废单，不能确认", 400)
        if o["status"] in ("shipped", "closed"):
            return err("该单已确认完或已关闭，不能再确认", 400)
        items = rows(c, "SELECT * FROM outbound_items WHERE order_id=?", (oid,))
        by_id = {it["id"]: it for it in items}
        req_items = b.get("items")
        if req_items is None:
            req_items = [{"item_id": it["id"],
                          "qty": it["qty"] - it["confirmed_qty"]} for it in items]
        now = datetime.now().isoformat()
        total_qty = 0
        total_amt = 0.0
        for ri in req_items:
            it = by_id.get(int(ri.get("item_id") or 0))
            if not it:
                return err("明细不存在", 400)
            q = int(ri.get("qty") or 0)
            remain = it["qty"] - it["confirmed_qty"]
            if q <= 0:
                return err("确认数量必须大于 0", 400)
            if q > remain:
                return err("确认数量超过剩余未出货数量", 400)
            have = get_stock(c, o["warehouse_id"], it["wine_id"], it["batch"] or "")
            if q > have:
                w = c.execute("SELECT name FROM wines WHERE id=?",
                              (it["wine_id"],)).fetchone()
                return err(f"库存不足: {w['name'] if w else it['wine_id']} "
                           f"现有 {have} 瓶, 要出 {q} 瓶")
            c.execute("UPDATE outbound_items SET confirmed_qty=confirmed_qty+?"
                      " WHERE id=?", (q, it["id"]))
            add_stock(c, o["warehouse_id"], it["wine_id"], it["batch"] or "", -q)
            amt = round(q * it["unit_price"], 2)
            prof = round(q * (it["unit_price"] - (it["cost_price"] or 0)), 2)
            c.execute("INSERT INTO order_confirms(order_type,order_id,item_id,qty,"
                      "amount,profit,created_at) VALUES('outbound',?,?,?,?,?,?)",
                      (oid, it["id"], q, amt, prof, now))
            total_qty += q
            total_amt += amt
        st = order_status_after(c, "outbound_orders", "outbound_items", oid, "shipped")
        c.execute("UPDATE outbound_orders SET status=? WHERE id=?", (st, oid))
        refresh_order_pay_status(c, "outbound_orders", "outbound_items", oid)
        audit(c, u["username"], "确认出货", o["order_no"],
              f"确认 {total_qty} 瓶, 金额 {total_amt:.2f}")
        return {"ok": True, "order_no": o["order_no"], "status": st,
                "confirmed_qty": total_qty, "confirmed_amount": round(total_amt, 2)}


@app.post("/api/transfers/{tid}/confirm")
async def confirm_transfer(tid: int, request: Request):
    """确认调拨: 整单确认, 确认后才调库存."""
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        tr = c.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
        if not tr:
            return err("单据不存在", 404)
        if tr["voided"]:
            return err("已废单，不能确认", 400)
        if tr["status"] == "done":
            return err("该调拨已完成，不能重复确认", 400)
        have = get_stock(c, tr["from_warehouse_id"], tr["wine_id"],
                         tr["batch"] or "")
        if tr["qty"] > have:
            return err(f"源仓库存不足, 现有 {have} 瓶")
        add_stock(c, tr["from_warehouse_id"], tr["wine_id"], tr["batch"] or "",
                  -tr["qty"])
        add_stock(c, tr["to_warehouse_id"], tr["wine_id"], tr["batch"] or "",
                  tr["qty"])
        c.execute("INSERT INTO order_confirms(order_type,order_id,item_id,qty,"
                  "amount,profit,created_at) VALUES('transfer',?,NULL,?,0,0,?)",
                  (tid, tr["qty"], datetime.now().isoformat()))
        c.execute("UPDATE transfers SET status='done' WHERE id=?", (tid,))
        audit(c, u["username"], "确认调拨", tr["transfer_no"], f"{tr['qty']} 瓶")
        return {"ok": True, "transfer_no": tr["transfer_no"], "status": "done"}


@app.post("/api/orders/{kind}/{oid}/void")
async def void_order(kind: str, oid: int, request: Request):
    """废单 (两段式新语义): 只取消未确认的部分, 已确认的部分保留.
    没确认过 -> 整单废 (status=voided); 部分确认过 -> 废掉剩余, 单据变已关闭;
    全部确认完 -> 400 不能废. 补登和自饮/赠送/破损是立即生效的单,
    废单按原单反向冲库存 (v2.3.0 的做法)."""
    u, e = await require_user(request)
    if e:
        return e
    if kind not in ("inbound", "outbound", "transfers"):
        return err("kind 只能是 inbound/outbound/transfers")
    now = datetime.now().isoformat()
    with conn() as c:
        if kind == "transfers":
            tr = c.execute("SELECT * FROM transfers WHERE id=?", (oid,)).fetchone()
            if not tr:
                return err("单据不存在", 404)
            if tr["voided"]:
                return err("该单据已废单，不能重复废单", 400)
            if tr["status"] == "done":
                return err("调拨已完成，不能废单", 400)
            c.execute("UPDATE transfers SET voided=1, voided_at=?, status='voided'"
                      " WHERE id=?", (now, oid))
            audit(c, u["username"], "废调拨", tr["transfer_no"],
                  f"{tr['qty']} 瓶")
            return {"ok": True, "transfer_no": tr["transfer_no"],
                    "status": "voided"}
        table = "inbound_orders" if kind == "inbound" else "outbound_orders"
        items_table = "inbound_items" if kind == "inbound" else "outbound_items"
        o = c.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
        if not o:
            return err("单据不存在", 404)
        if o["voided"]:
            return err("该单据已废单，不能重复废单", 400)
        if o["status"] == "closed":
            return err("该单据已关闭，不能重复废单", 400)
        immediate = (kind == "inbound" and o["type"] == "opening") or \
            (kind == "outbound" and o["type"] != "sale")
        if immediate:
            # 立即生效的单: 废单 = 按原单反向冲库存
            items = rows(c, f"SELECT * FROM {items_table} WHERE order_id=?", (oid,))
            if kind == "inbound":
                for it in items:
                    add_stock(c, o["warehouse_id"], it["wine_id"],
                              it["batch"] or "", -it["qty"])
                action = "废补登"
            else:
                for it in items:
                    add_stock(c, o["warehouse_id"], it["wine_id"],
                              it["batch"] or "", it["qty"])
                action = {"personal": "废自饮", "gift": "废赠送",
                          "damage": "废破损"}[o["type"]]
            c.execute(f"UPDATE {table} SET voided=1, voided_at=?, status='voided'"
                      " WHERE id=?", (now, oid))
            audit(c, u["username"], action, o["order_no"], "")
            return {"ok": True, "order_no": o["order_no"], "status": "voided"}
        # 两段式: 按已确认进度决定
        r = c.execute(f"SELECT COALESCE(SUM(qty),0) q,"
                      f" COALESCE(SUM(confirmed_qty),0) cq FROM {items_table}"
                      f" WHERE order_id=?", (oid,)).fetchone()
        if r["cq"] + 1e-9 >= r["q"]:
            return err("该单已全部确认，不能废单", 400)
        if r["cq"] <= 0:
            c.execute(f"UPDATE {table} SET voided=1, voided_at=?, status='voided'"
                      " WHERE id=?", (now, oid))
            action = "废进货" if kind == "inbound" else "废出货"
            audit(c, u["username"], action, o["order_no"], "未确认过，整单废")
            return {"ok": True, "order_no": o["order_no"], "status": "voided"}
        c.execute(f"UPDATE {table} SET status='closed' WHERE id=?", (oid,))
        action = "废进货剩余" if kind == "inbound" else "废出货剩余"
        audit(c, u["username"], action, o["order_no"],
              f"已确认 {int(r['cq'])} 瓶保留，剩余取消")
        return {"ok": True, "order_no": o["order_no"], "status": "closed"}


@app.get("/api/stock")
async def list_stock(request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        return rows(c, "SELECT s.*, w.name AS wine_name, wh.name AS warehouse_name"
                       " FROM stock s JOIN wines w ON w.id=s.wine_id"
                       " JOIN warehouses wh ON wh.id=s.warehouse_id"
                       " WHERE s.quantity>0 ORDER BY w.name")

# ---------- 分享图 ----------
def render_share_png(lines, title):
    from PIL import Image, ImageDraw, ImageFont
    W = 1080
    H = 120 + 64 * len(lines) + 80
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    import glob
    cjk = (glob.glob("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.*")
           + glob.glob("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.*")
           + glob.glob(str(BASE_DIR / "static" / "fonts" / "NotoSansSC*")))
    dejavu = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    dejavu_b = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    f_body_path = cjk[0] if cjk else dejavu
    f_title_path = cjk[0] if cjk else dejavu_b
    try:
        f_title = ImageFont.truetype(f_title_path, 52)
        f_body = ImageFont.truetype(f_body_path, 40)
    except Exception:
        f_title = ImageFont.load_default()
        f_body = ImageFont.load_default()
    d.text((60, 30), title, fill="black", font=f_title)
    d.line([(60, 110), (W - 60, 110)], fill="black", width=3)
    y = 140
    for line in lines:
        d.text((60, y), line, fill="black", font=f_body)
        y += 64
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@app.get("/api/orders/{otype}/{oid}/share.png")
async def share_png(otype: str, oid: int, request: Request):
    u, e = await require_user(request)
    if e:
        return e
    with conn() as c:
        if otype == "outbound":
            d = order_detail(c, "outbound_orders", "outbound_items", oid)
            if not d:
                return err("单据不存在", 404)
            if d.get("voided"):
                return err("已作废的单据不能生成分享图", 400)
            if (d.get("type") or "sale") != "sale":
                return err("自饮/赠送/破损单不需要分享图", 400)
            cust = c.execute("SELECT name FROM customers WHERE id=?",
                             (d["customer_id"],)).fetchone()
            lines = [f"单号: {d['order_no']}", f"日期: {d['order_date']}",
                     f"客户: {cust['name'] if cust else ''}"]
            for it in d["items"]:
                if (it.get("discount_amount") or 0) > 0:
                    lines.append(f"{it['wine_name']} {it.get('spec') or ''} x{it['qty']} "
                                 f"原价{it['orig_price']:.2f} 折后{it['unit_price']:.2f} "
                                 f"折扣{it['discount_amount']:.2f} 小计{it['qty']*it['unit_price']:.2f}")
                else:
                    lines.append(f"{it['wine_name']} {it.get('spec') or ''} x{it['qty']} "
                                 f"单价{it['unit_price']:.2f} 小计{it['qty']*it['unit_price']:.2f}")
            total = d["sale_total"]
            lines += [f"货款合计: {total:.2f} 元"]
            if (d.get("discount_total") or 0) > 0:
                lines.append(f"其中折扣: {d['discount_total']:.2f} 元"
                             f" (原价合计 {d.get('orig_total') or 0:.2f} 元)")
                reasons = sorted({(it.get("reason") or "") for it in d["items"]
                                  if it.get("reason")})
                if reasons:
                    lines.append("折扣原因: " + "、".join(reasons))
            lines += [f"物流: {d.get('carrier') or ''} {d.get('tracking_no') or ''} "
                      f"运费{d['freight']:.2f} 元",
                      f"已付: {d['paid_amount']:.2f} 元  还欠: {max(total-d['paid_amount'],0):.2f} 元"]
            return Response(render_share_png(lines, "出货单"), media_type="image/png")
        if otype == "inbound":
            d = order_detail(c, "inbound_orders", "inbound_items", oid)
            if not d:
                return err("单据不存在", 404)
            if d.get("voided"):
                return err("已作废的单据不能生成分享图", 400)
            sup = c.execute("SELECT name FROM suppliers WHERE id=?",
                            (d["supplier_id"],)).fetchone()
            kind = "补登单" if d["type"] == "opening" else "进货单"
            lines = [f"单号: {d['order_no']}", f"日期: {d['order_date']}",
                     f"供应商: {sup['name'] if sup else ''}"]
            for it in d["items"]:
                lines.append(f"{it['wine_name']} {it.get('spec') or ''} x{it['qty']} "
                             f"单价{it['unit_price']:.2f} 小计{it['qty']*it['unit_price']:.2f}")
            total = d["goods_total"]
            lines += [f"货款合计: {total:.2f} 元",
                      f"物流: {d.get('carrier') or ''} {d.get('tracking_no') or ''} "
                      f"运费{d['freight']:.2f} 元",
                      f"已付: {d['paid_amount']:.2f} 元  还欠: {max(total-d['paid_amount'],0):.2f} 元"]
            return Response(render_share_png(lines, kind), media_type="image/png")
        return err("otype 只能是 inbound/outbound")


app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    init_db()
    # 端口定为 9000（Tristan 确认）；可用环境变量 PORT 覆盖，不用改代码
    port = int(os.environ.get("PORT", "9000"))
    uvicorn.run(app, host="127.0.0.1", port=port)
