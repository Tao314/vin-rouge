#!/usr/bin/env python3
"""红酒进销存: FastAPI + SQLite(WAL).

v0.3.0: 档案、采购入库(含运费分摊/期初库存/入库付款状态)、批次库存+流水、
多仓调拨、应付与付款、客户货单(预定/发货扣库存/取消留痕/公开单页)、
注册与管理员; 已录入信息可更改(档案/货单/入库单/调拨/付款, 带逻辑把关与修改留痕)。
拍照扫码/直发/退货为后续版本。
金额一律整数分存储; 库存以流水为据、余额表为缓存; 单据带幂等键防重复提交。
老库兼容: 启动时只做 CREATE TABLE IF NOT EXISTS + 检测后 ALTER ADD COLUMN(带默认值),
绝不删列/改列类型, 老数据与老管理员账号原样保留。
"""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("WINE_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("WINE_DB_PATH", DATA_DIR / "wine.db"))
STATIC_DIR = BASE_DIR / "static"
APP_VERSION = "v0.4.0"
SESSION_DAYS = 30
ADMIN_USER = os.environ.get("WINE_ADMIN_USER", "allen314")
# 不写死密码: 优先环境变量 WINE_ADMIN_PASSWORD; 未设时首次启动随机生成,
# 打印到启动日志并写到 data/INITIAL_PASSWORD.txt(该目录被 .gitignore 排除),
# 登录后请立即在「设置」里改密码并删除该文件。
ADMIN_PASS = os.environ.get("WINE_ADMIN_PASSWORD", "").strip()

app = FastAPI(title="红酒进销存·入库MVP")


# ---------------------------------------------------------------- DB
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()


def _add_column(c, table, col, ddl):
    """老库兼容迁移: 列不存在才 ADD COLUMN(带默认值), 绝不删列/改类型。"""
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    with db() as c:
        # 先记录老库状态(老库升级时 users 表已存在), 供管理员兼容逻辑判断
        had_users_table = bool(c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone())
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                pw_hash TEXT NOT NULL, salt TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL DEFAULT '酒',
                name TEXT NOT NULL, category TEXT DEFAULT '',
                vintage TEXT DEFAULT '', origin TEXT DEFAULT '',
                note TEXT DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS suppliers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, contact TEXT DEFAULT '',
                phone TEXT DEFAULT '', note TEXT DEFAULT '',
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS customers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, contact TEXT DEFAULT '',
                phone TEXT DEFAULT '', note TEXT DEFAULT '',
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS warehouses(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, note TEXT DEFAULT '',
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS purchase_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT UNIQUE NOT NULL,
                type TEXT NOT NULL DEFAULT '采购入库',
                supplier_id INTEGER, warehouse_id INTEGER NOT NULL,
                biz_date TEXT NOT NULL,
                freight_cents INTEGER NOT NULL DEFAULT 0,
                goods_cents INTEGER NOT NULL DEFAULT 0,
                total_cents INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '已确认',
                idempotency_key TEXT UNIQUE,
                note TEXT DEFAULT '', created_by TEXT DEFAULT '',
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS purchase_lines(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
                qty INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL,
                line_total_cents INTEGER NOT NULL,
                freight_share_cents INTEGER NOT NULL DEFAULT 0,
                batch_id INTEGER);
            CREATE TABLE IF NOT EXISTS batches(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL, supplier_id INTEGER,
                purchase_line_id INTEGER,
                qty_initial INTEGER NOT NULL,
                unit_price_cents INTEGER NOT NULL,
                total_cost_cents INTEGER NOT NULL,
                biz_date TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inventory_balances(
                batch_id INTEGER NOT NULL, warehouse_id INTEGER NOT NULL,
                qty INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(batch_id, warehouse_id));
            CREATE TABLE IF NOT EXISTS inventory_txns(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                txn_type TEXT NOT NULL, product_id INTEGER NOT NULL,
                batch_id INTEGER, warehouse_id INTEGER,
                qty_delta INTEGER NOT NULL,
                ref_type TEXT DEFAULT '', ref_id INTEGER,
                note TEXT DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS payables(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER NOT NULL,
                purchase_order_id INTEGER UNIQUE NOT NULL,
                amount_cents INTEGER NOT NULL,
                paid_cents INTEGER NOT NULL DEFAULT 0,
                biz_date TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS payments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payable_id INTEGER NOT NULL, supplier_id INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL, pay_date TEXT NOT NULL,
                note TEXT DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transfers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transfer_no TEXT UNIQUE NOT NULL,
                from_warehouse_id INTEGER NOT NULL,
                to_warehouse_id INTEGER NOT NULL,
                biz_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '已确认',
                idempotency_key TEXT UNIQUE,
                note TEXT DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS transfer_lines(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transfer_id INTEGER NOT NULL, batch_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL, qty INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS sales_orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT UNIQUE NOT NULL,
                customer_id INTEGER NOT NULL, warehouse_id INTEGER NOT NULL,
                biz_date TEXT NOT NULL DEFAULT '',
                freight_cents INTEGER NOT NULL DEFAULT 0,
                freight_payer TEXT NOT NULL DEFAULT '客户付',
                goods_cents INTEGER NOT NULL DEFAULT 0,
                total_cents INTEGER NOT NULL DEFAULT 0,
                order_status TEXT NOT NULL DEFAULT '正常',
                ship_status TEXT NOT NULL DEFAULT '未发货',
                pay_status TEXT NOT NULL DEFAULT '未付款',
                pay_time TEXT, ship_time TEXT,
                logistics_no TEXT DEFAULT '',
                public_token TEXT UNIQUE,
                idempotency_key TEXT UNIQUE,
                note TEXT DEFAULT '', cancel_reason TEXT DEFAULT '',
                cancelled_at TEXT, created_by TEXT DEFAULT '',
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sales_lines(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
                qty INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL DEFAULT 0,
                line_type TEXT NOT NULL DEFAULT '销售',
                line_total_cents INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sales_allocations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL, line_id INTEGER NOT NULL,
                batch_id INTEGER NOT NULL, qty INTEGER NOT NULL,
                cost_cents INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sales_order_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL, event TEXT NOT NULL,
                detail TEXT DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS edit_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
                before_json TEXT NOT NULL DEFAULT '{}',
                after_json TEXT NOT NULL DEFAULT '{}',
                operator TEXT DEFAULT '', created_at TEXT NOT NULL);
            """
        )
        # ---- 老库兼容迁移: 只加列(带默认值), 不删列/不改类型 ----
        _add_column(c, "users", "role", "role TEXT NOT NULL DEFAULT 'admin'")
        _add_column(c, "purchase_orders", "pay_status", "pay_status TEXT NOT NULL DEFAULT '未付款'")
        _add_column(c, "customers", "address", "address TEXT DEFAULT ''")
        # 管理员兼容: 老库(已有 users 表)或设了 WINE_ADMIN_PASSWORD 时, 沿用老逻辑
        # 自动建 allen314(环境变量密码 / INITIAL_PASSWORD.txt), 老管理员绝不登不进去。
        # 全新库且未设环境变量时不自动建号, 改走注册: 第一个注册者自动管理员。
        row = c.execute("SELECT id FROM users WHERE username=?", (ADMIN_USER,)).fetchone()
        user_count = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        _bm = c.execute("SELECT value FROM settings WHERE key='bootstrap_mode'").fetchone()
        bootstrap_mode = _bm["value"] if _bm else ""
        # 全新库(无老表、无环境变量密码)走注册模式并记标记, 防止下次启动误建 allen314
        if not row and user_count == 0 and not ADMIN_PASS and not had_users_table and not bootstrap_mode:
            c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('bootstrap_mode','register')")
            bootstrap_mode = "register"
        if not row and user_count == 0 and bootstrap_mode != "register" and (had_users_table or ADMIN_PASS):
            pw = ADMIN_PASS or secrets.token_urlsafe(12)
            salt = secrets.token_hex(8)
            c.execute(
                "INSERT INTO users(username,pw_hash,salt,role,created_at) VALUES(?,?,?,?,?)",
                (ADMIN_USER, hash_pw(pw, salt), salt, "admin", now_iso()),
            )
            if not ADMIN_PASS:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                (DATA_DIR / "INITIAL_PASSWORD.txt").write_text(
                    f"账号: {ADMIN_USER}\n初始密码: {pw}\n登录后请立即改密码并删除本文件。\n",
                    encoding="utf-8",
                )
                print(f"[wine] 首次启动: 管理员 {ADMIN_USER} 初始密码已生成, 见 {DATA_DIR / 'INITIAL_PASSWORD.txt'}", flush=True)


init_db()


# ---------------------------------------------------------------- 工具
def to_cents(yuan) -> int:
    try:
        d = Decimal(str(yuan))
    except (InvalidOperation, ValueError):
        raise HTTPException(400, "金额格式不对")
    if d < 0:
        raise HTTPException(400, "金额不能为负")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def yuan(cents) -> float:
    return round(int(cents) / 100, 2)


def current_user(token: Optional[str]):
    if not token:
        return None
    with db() as c:
        row = c.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token=? AND s.expires_at>?",
            (token, now_iso()),
        ).fetchone()
    return dict(row) if row else None


def require_user(token: Optional[str]):
    u = current_user(token)
    if not u:
        raise HTTPException(401, "未登录")
    return u


def get_setting(c, key, default=""):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def registration_open(c) -> bool:
    """注册开关: 管理员显式设置优先; 未设置时默认无用户开、有用户关。"""
    v = get_setting(c, "registration_open", "")
    if v in ("1", "0"):
        return v == "1"
    return c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def reserved_qty(c, product_id, warehouse_id=None) -> int:
    """已预定: 正常且未发货货单的明细数量(可按仓库过滤)。"""
    q = ("SELECT COALESCE(SUM(l.qty),0) FROM sales_lines l "
         "JOIN sales_orders o ON o.id=l.order_id "
         "WHERE l.product_id=? AND o.order_status='正常' AND o.ship_status='未发货'")
    args = [product_id]
    if warehouse_id is not None:
        q += " AND o.warehouse_id=?"
        args.append(warehouse_id)
    return c.execute(q, args).fetchone()[0]


def stock_qty(c, product_id, warehouse_id) -> int:
    return c.execute(
        "SELECT COALESCE(SUM(bal.qty),0) FROM inventory_balances bal "
        "JOIN batches b ON b.id=bal.batch_id WHERE b.product_id=? AND bal.warehouse_id=?",
        (product_id, warehouse_id)).fetchone()[0]


def next_no(c, prefix: str, table: str, col: str) -> str:
    day = datetime.now().strftime("%Y%m%d")
    n = c.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE ?", (f"{prefix}{day}-%",)).fetchone()[0]
    return f"{prefix}{day}-{n + 1:03d}"


def alloc_freight(freight: int, weights: list[int]) -> list[int]:
    """按权重(行金额, 全为0时按数量)分摊运费(分), 余数逐分补给前面的行。"""
    if freight <= 0:
        return [0] * len(weights)
    total = sum(weights)
    if total <= 0:
        weights = [1] * len(weights)
        total = len(weights)
    shares = [freight * w // total for w in weights]
    rest = freight - sum(shares)
    i = 0
    while rest > 0:
        shares[i % len(shares)] += 1
        rest -= 1
        i += 1
    return shares


def log_edit(c, entity_type, entity_id, before, after, operator):
    """修改留痕: 每次成功修改写一条改前/改后 JSON 快照。"""
    c.execute(
        "INSERT INTO edit_logs(entity_type,entity_id,before_json,after_json,operator,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (entity_type, entity_id,
         json.dumps(before, ensure_ascii=False, default=str),
         json.dumps(after, ensure_ascii=False, default=str),
         operator or "", now_iso()))


# ---------------------------------------------------------------- 模型
class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str
    password: str


class RegistrationSettingIn(BaseModel):
    open: bool


class ChangePwIn(BaseModel):
    old_password: str
    new_password: str


class ProductIn(BaseModel):
    type: str = "酒"
    name: str
    category: str = ""
    vintage: str = ""
    origin: str = ""
    note: str = ""


class PartyIn(BaseModel):
    name: str
    contact: str = ""
    phone: str = ""
    note: str = ""


class CustomerIn(BaseModel):
    # 客户信息: 姓名/电话/地址/备注(contact 列保留兼容老数据, 不再录入)
    name: str
    phone: str = ""
    address: str = ""
    note: str = ""


class WarehouseIn(BaseModel):
    name: str
    note: str = ""


class PurchaseLineIn(BaseModel):
    product_id: int
    qty: int
    unit_price: str  # 元, 字符串避免浮点


class PurchaseIn(BaseModel):
    type: str = "采购入库"  # 采购入库 / 期初库存
    supplier_id: Optional[int] = None
    warehouse_id: int
    biz_date: str
    freight: str = "0"
    lines: list[PurchaseLineIn]
    note: str = ""
    idempotency_key: str = ""
    pay_status: str = "未付款"  # 已付款 / 未付款(仅采购入库; 已付款=应付立即全额核销)


class SalesLineIn(BaseModel):
    product_id: int
    qty: int
    unit_price: str = "0"  # 元; 赠送行强制 0
    line_type: str = "销售"  # 销售 / 赠送


class SalesIn(BaseModel):
    customer_id: int
    warehouse_id: int
    biz_date: str = ""
    lines: list[SalesLineIn]
    freight: str = "0"
    freight_payer: str = "客户付"  # 客户付 / 自提无 / 我方代付
    pay_status: str = "未付款"  # 已付款(预付) / 未付款
    logistics_no: str = ""
    note: str = ""
    idempotency_key: str = ""


class ShipIn(BaseModel):
    logistics_no: str = ""
    ship_time: str = ""


class PayStatusIn(BaseModel):
    pay_status: str  # 已付款 / 未付款


class CancelIn(BaseModel):
    reason: str = ""


class TransferLineIn(BaseModel):
    batch_id: int
    qty: int


class TransferIn(BaseModel):
    from_warehouse_id: int
    to_warehouse_id: int
    biz_date: str
    lines: list[TransferLineIn]
    note: str = ""
    idempotency_key: str = ""


class PaymentIn(BaseModel):
    amount: str
    pay_date: str
    note: str = ""


class PurchaseEditIn(BaseModel):
    supplier_id: Optional[int] = None
    warehouse_id: int
    biz_date: str
    freight: str = "0"
    lines: list[PurchaseLineIn]
    note: str = ""
    pay_status: str = "未付款"


class SalesEditIn(BaseModel):
    customer_id: int
    warehouse_id: int
    biz_date: str = ""
    lines: list[SalesLineIn]
    freight: str = "0"
    freight_payer: str = "客户付"
    logistics_no: str = ""
    note: str = ""


class TransferEditIn(BaseModel):
    to_warehouse_id: int
    biz_date: str
    lines: list[TransferLineIn]
    note: str = ""


# ---------------------------------------------------------------- 静态/登录
@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/login")
def login(body: LoginIn, response: Response):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
        if not row or not secrets.compare_digest(hash_pw(body.password, row["salt"]), row["pw_hash"]):
            raise HTTPException(401, "账号或密码不对")
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
        c.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES(?,?,?)", (token, row["id"], exp))
    response.set_cookie("wine_token", token, httponly=True, samesite="lax", max_age=SESSION_DAYS * 86400)
    return {"ok": True, "username": body.username}


@app.post("/api/logout")
def logout(response: Response, wine_token: Optional[str] = Cookie(default=None)):
    if wine_token:
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (wine_token,))
    response.delete_cookie("wine_token")
    return {"ok": True}


@app.get("/api/me")
def me(wine_token: Optional[str] = Cookie(default=None)):
    u = current_user(wine_token)
    with db() as c:
        reg_open = registration_open(c)
    return {"logged_in": bool(u), "username": u["username"] if u else None,
            "role": (u.get("role") or "admin") if u else None,
            "registration_open": reg_open, "version": APP_VERSION}


@app.get("/api/registration-status")
def registration_status():
    with db() as c:
        return {"open": registration_open(c),
                "has_users": c.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0}


@app.post("/api/register")
def register(body: RegisterIn):
    username = body.username.strip()
    if not username:
        raise HTTPException(400, "用户名不能为空")
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少6位")
    with db() as c:
        if not registration_open(c):
            raise HTTPException(403, "注册已关闭")
        if c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(400, "用户名已存在")
        # 本期注册用户角色只做管理员一种(role 字段预留); 第一个注册者自动管理员
        salt = secrets.token_hex(8)
        cur = c.execute(
            "INSERT INTO users(username,pw_hash,salt,role,created_at) VALUES(?,?,?,?,?)",
            (username, hash_pw(body.password, salt), salt, "admin", now_iso()))
        return {"ok": True, "id": cur.lastrowid, "username": username, "role": "admin"}


@app.post("/api/settings/registration")
def set_registration(body: RegistrationSettingIn, wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('registration_open',?)",
                  ("1" if body.open else "0",))
    return {"ok": True, "registration_open": body.open}


@app.post("/api/change-password")
def change_pw(body: ChangePwIn, wine_token: Optional[str] = Cookie(default=None)):
    u = require_user(wine_token)
    if len(body.new_password) < 6:
        raise HTTPException(400, "新密码至少6位")
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        if not secrets.compare_digest(hash_pw(body.old_password, row["salt"]), row["pw_hash"]):
            raise HTTPException(400, "原密码不对")
        salt = secrets.token_hex(8)
        c.execute("UPDATE users SET pw_hash=?,salt=? WHERE id=?", (hash_pw(body.new_password, salt), salt, u["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- 档案
def _list(table, token):
    require_user(token)
    with db() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY id")]


@app.get("/api/products")
def products(wine_token: Optional[str] = Cookie(default=None)):
    return _list("products", wine_token)


@app.post("/api/products")
def add_product(body: ProductIn, wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    if body.type not in ("酒", "附件"):
        raise HTTPException(400, "类型只能是酒或附件")
    if not body.name.strip():
        raise HTTPException(400, "名字不能为空")
    with db() as c:
        cur = c.execute(
            "INSERT INTO products(type,name,category,vintage,origin,note,created_at) VALUES(?,?,?,?,?,?,?)",
            (body.type, body.name.strip(), body.category.strip(), body.vintage.strip(),
             body.origin.strip(), body.note.strip(), now_iso()),
        )
        return {"id": cur.lastrowid}


@app.put("/api/products/{pid}")
def edit_product(pid: int, body: ProductIn, wine_token: Optional[str] = Cookie(default=None)):
    u = require_user(wine_token)
    if body.type not in ("酒", "附件"):
        raise HTTPException(400, "类型只能是酒或附件")
    if not body.name.strip():
        raise HTTPException(400, "名字不能为空")
    with db() as c:
        old = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not old:
            raise HTTPException(404, "商品不存在")
        c.execute(
            "UPDATE products SET type=?,name=?,category=?,vintage=?,origin=?,note=? WHERE id=?",
            (body.type, body.name.strip(), body.category.strip(), body.vintage.strip(),
             body.origin.strip(), body.note.strip(), pid))
        new = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        log_edit(c, "product", pid, dict(old), dict(new), u["username"])
    return {"ok": True, "id": pid}


@app.get("/api/suppliers")
def suppliers(wine_token: Optional[str] = Cookie(default=None)):
    return _list("suppliers", wine_token)


@app.post("/api/suppliers")
def add_supplier(body: PartyIn, wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    if not body.name.strip():
        raise HTTPException(400, "名字不能为空")
    with db() as c:
        cur = c.execute(
            "INSERT INTO suppliers(name,phone,note,created_at) VALUES(?,?,?,?)",
            (body.name.strip(), body.phone.strip(), body.note.strip(), now_iso()),
        )
        return {"id": cur.lastrowid}


@app.put("/api/suppliers/{sid}")
def edit_supplier(sid: int, body: PartyIn, wine_token: Optional[str] = Cookie(default=None)):
    u = require_user(wine_token)
    if not body.name.strip():
        raise HTTPException(400, "名字不能为空")
    with db() as c:
        old = c.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if not old:
            raise HTTPException(404, "供应商不存在")
        c.execute(
            "UPDATE suppliers SET name=?,phone=?,note=? WHERE id=?",
            (body.name.strip(), body.phone.strip(), body.note.strip(), sid))
        new = c.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        log_edit(c, "supplier", sid, dict(old), dict(new), u["username"])
    return {"ok": True, "id": sid}


@app.get("/api/customers")
def customers(wine_token: Optional[str] = Cookie(default=None)):
    return _list("customers", wine_token)


@app.post("/api/customers")
def add_customer(body: CustomerIn, wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    if not body.name.strip():
        raise HTTPException(400, "名字不能为空")
    with db() as c:
        cur = c.execute(
            "INSERT INTO customers(name,phone,address,note,created_at) VALUES(?,?,?,?,?)",
            (body.name.strip(), body.phone.strip(), body.address.strip(), body.note.strip(), now_iso()),
        )
        return {"id": cur.lastrowid}


@app.put("/api/customers/{cid}")
def edit_customer(cid: int, body: CustomerIn, wine_token: Optional[str] = Cookie(default=None)):
    u = require_user(wine_token)
    if not body.name.strip():
        raise HTTPException(400, "名字不能为空")
    with db() as c:
        old = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        if not old:
            raise HTTPException(404, "客户不存在")
        c.execute(
            "UPDATE customers SET name=?,phone=?,address=?,note=? WHERE id=?",
            (body.name.strip(), body.phone.strip(), body.address.strip(), body.note.strip(), cid))
        new = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        log_edit(c, "customer", cid, dict(old), dict(new), u["username"])
    return {"ok": True, "id": cid}


@app.get("/api/warehouses")
def warehouses(wine_token: Optional[str] = Cookie(default=None)):
    return _list("warehouses", wine_token)


@app.post("/api/warehouses")
def add_warehouse(body: WarehouseIn, wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    if not body.name.strip():
        raise HTTPException(400, "名字不能为空")
    with db() as c:
        cur = c.execute(
            "INSERT INTO warehouses(name,note,created_at) VALUES(?,?,?)",
            (body.name.strip(), body.note.strip(), now_iso()),
        )
        return {"id": cur.lastrowid}


@app.put("/api/warehouses/{wid}")
def edit_warehouse(wid: int, body: WarehouseIn, wine_token: Optional[str] = Cookie(default=None)):
    u = require_user(wine_token)
    if not body.name.strip():
        raise HTTPException(400, "名字不能为空")
    with db() as c:
        old = c.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone()
        if not old:
            raise HTTPException(404, "仓库不存在")
        c.execute("UPDATE warehouses SET name=?,note=? WHERE id=?",
                  (body.name.strip(), body.note.strip(), wid))
        new = c.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone()
        log_edit(c, "warehouse", wid, dict(old), dict(new), u["username"])
    return {"ok": True, "id": wid}


# ---------------------------------------------------------------- 采购入库
@app.post("/api/purchase-orders")
def create_purchase(body: PurchaseIn, wine_token: Optional[str] = Cookie(default=None)):
    u = require_user(wine_token)
    if body.type not in ("采购入库", "期初库存"):
        raise HTTPException(400, "单据类型不对")
    if body.type == "采购入库" and not body.supplier_id:
        raise HTTPException(400, "采购入库必须选供应商")
    if body.pay_status not in ("已付款", "未付款"):
        raise HTTPException(400, "付款状态不对")
    if not body.lines:
        raise HTTPException(400, "至少一行明细")
    freight = to_cents(body.freight or "0")
    lines = []
    for ln in body.lines:
        if ln.qty <= 0:
            raise HTTPException(400, "数量必须大于0")
        up = to_cents(ln.unit_price)
        lines.append((ln.product_id, ln.qty, up, up * ln.qty))
    goods = sum(x[3] for x in lines)
    weights = [x[3] for x in lines] if goods > 0 else [x[1] for x in lines]
    shares = alloc_freight(freight, weights)
    with db() as c:
        if body.idempotency_key:
            old = c.execute(
                "SELECT id,order_no FROM purchase_orders WHERE idempotency_key=?",
                (body.idempotency_key,),
            ).fetchone()
            if old:
                return {"id": old["id"], "order_no": old["order_no"], "duplicated": True}
        if not c.execute("SELECT id FROM warehouses WHERE id=?", (body.warehouse_id,)).fetchone():
            raise HTTPException(400, "仓库不存在")
        if body.supplier_id and not c.execute(
            "SELECT id FROM suppliers WHERE id=?", (body.supplier_id,)
        ).fetchone():
            raise HTTPException(400, "供应商不存在")
        for pid, *_ in lines:
            if not c.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone():
                raise HTTPException(400, "商品不存在")
        order_no = next_no(c, "RK", "purchase_orders", "order_no")
        cur = c.execute(
            "INSERT INTO purchase_orders(order_no,type,supplier_id,warehouse_id,biz_date,"
            "freight_cents,goods_cents,total_cents,status,idempotency_key,note,created_by,created_at,pay_status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_no, body.type, body.supplier_id, body.warehouse_id, body.biz_date,
             freight, goods, goods + freight, "已确认", body.idempotency_key or None,
             body.note.strip(), u["username"], now_iso(),
             body.pay_status if body.type == "采购入库" else "未付款"),
        )
        oid = cur.lastrowid
        for (pid, qty, up, lt), share in zip(lines, shares):
            bcur = c.execute(
                "INSERT INTO batches(product_id,supplier_id,purchase_line_id,qty_initial,"
                "unit_price_cents,total_cost_cents,biz_date,created_at) VALUES(?,?,NULL,?,?,?,?,?)",
                (pid, body.supplier_id, qty, up, lt + share, body.biz_date, now_iso()),
            )
            bid = bcur.lastrowid
            lcur = c.execute(
                "INSERT INTO purchase_lines(order_id,product_id,qty,unit_price_cents,"
                "line_total_cents,freight_share_cents,batch_id) VALUES(?,?,?,?,?,?,?)",
                (oid, pid, qty, up, lt, share, bid),
            )
            c.execute("UPDATE batches SET purchase_line_id=? WHERE id=?", (lcur.lastrowid, bid))
            c.execute(
                "INSERT INTO inventory_balances(batch_id,warehouse_id,qty) VALUES(?,?,?)",
                (bid, body.warehouse_id, qty),
            )
            c.execute(
                "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("期初" if body.type == "期初库存" else "入库", pid, bid, body.warehouse_id,
                 qty, "purchase_order", oid, order_no, now_iso()),
            )
        if body.type == "采购入库":
            pcur = c.execute(
                "INSERT INTO payables(supplier_id,purchase_order_id,amount_cents,biz_date,created_at)"
                " VALUES(?,?,?,?,?)",
                (body.supplier_id, oid, goods + freight, body.biz_date, now_iso()),
            )
            if body.pay_status == "已付款" and goods + freight > 0:
                # 已付款: 应付生成后立即记一笔全额付款核销, 余额为 0
                c.execute(
                    "INSERT INTO payments(payable_id,supplier_id,amount_cents,pay_date,note,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (pcur.lastrowid, body.supplier_id, goods + freight, body.biz_date,
                     "入库时已付款", now_iso()))
                c.execute("UPDATE payables SET paid_cents=? WHERE id=?",
                          (goods + freight, pcur.lastrowid))
    return {"id": oid, "order_no": order_no, "duplicated": False}


@app.get("/api/purchase-orders")
def list_purchases(wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    with db() as c:
        rows = c.execute(
            "SELECT o.*, s.name supplier_name, w.name warehouse_name FROM purchase_orders o "
            "LEFT JOIN suppliers s ON s.id=o.supplier_id "
            "LEFT JOIN warehouses w ON w.id=o.warehouse_id ORDER BY o.id DESC LIMIT 100"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["lines"] = [dict(x) for x in c.execute(
                "SELECT l.*, p.name product_name FROM purchase_lines l "
                "JOIN products p ON p.id=l.product_id WHERE l.order_id=?", (d["id"],))]
            out.append(d)
        return out


@app.post("/api/purchase-orders/{oid}/cancel")
def cancel_purchase(oid: int, wine_token: Optional[str] = Cookie(default=None)):
    """作废重录: 仅当未付款且批次库存未动(未调拨)时可作废, 冲销流水留痕。"""
    require_user(wine_token)
    with db() as c:
        o = c.execute("SELECT * FROM purchase_orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise HTTPException(404, "单据不存在")
        if o["status"] == "已作废":
            raise HTTPException(400, "已作废")
        pay = c.execute("SELECT * FROM payables WHERE purchase_order_id=?", (oid,)).fetchone()
        if pay and pay["paid_cents"] > 0:
            raise HTTPException(400, "已付款,不能作废")
        lines = c.execute("SELECT * FROM purchase_lines WHERE order_id=?", (oid,)).fetchall()
        for ln in lines:
            bal = c.execute(
                "SELECT COALESCE(SUM(qty),0) q FROM inventory_balances WHERE batch_id=?",
                (ln["batch_id"],)).fetchone()["q"]
            if bal != ln["qty"]:
                raise HTTPException(400, "库存已变动,不能作废")
        for ln in lines:
            c.execute("DELETE FROM inventory_balances WHERE batch_id=?", (ln["batch_id"],))
            c.execute(
                "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("作废冲销", ln["product_id"], ln["batch_id"], o["warehouse_id"], -ln["qty"],
                 "purchase_order", oid, o["order_no"], now_iso()),
            )
        if pay:
            c.execute("DELETE FROM payables WHERE id=?", (pay["id"],))
        c.execute("UPDATE purchase_orders SET status='已作废' WHERE id=?", (oid,))
    return {"ok": True}


@app.put("/api/purchase-orders/{oid}")
def edit_purchase(oid: int, body: PurchaseEditIn, wine_token: Optional[str] = Cookie(default=None)):
    """改入库单: 仅当本单批次未被出库消耗/未调拨时可改; 批次成本与应付重算。

    把关: 已有付款时新应付总额不得小于已付; 改后库存不得小于已预定数量。
    """
    u = require_user(wine_token)
    if body.pay_status not in ("已付款", "未付款"):
        raise HTTPException(400, "付款状态不对")
    if not body.lines:
        raise HTTPException(400, "至少一行明细")
    freight = to_cents(body.freight or "0")
    lines = []
    for ln in body.lines:
        if ln.qty <= 0:
            raise HTTPException(400, "数量必须大于0")
        up = to_cents(ln.unit_price)
        lines.append((ln.product_id, ln.qty, up, up * ln.qty))
    goods = sum(x[3] for x in lines)
    weights = [x[3] for x in lines] if goods > 0 else [x[1] for x in lines]
    shares = alloc_freight(freight, weights)
    new_total = goods + freight
    with db() as c:
        o = c.execute("SELECT * FROM purchase_orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise HTTPException(404, "单据不存在")
        if o["status"] == "已作废":
            raise HTTPException(400, "已作废的单不可改")
        if o["type"] == "采购入库" and not body.supplier_id:
            raise HTTPException(400, "采购入库必须选供应商")
        if not c.execute("SELECT id FROM warehouses WHERE id=?", (body.warehouse_id,)).fetchone():
            raise HTTPException(400, "仓库不存在")
        if body.supplier_id and not c.execute(
            "SELECT id FROM suppliers WHERE id=?", (body.supplier_id,)).fetchone():
            raise HTTPException(400, "供应商不存在")
        for pid, *_ in lines:
            if not c.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone():
                raise HTTPException(400, "商品不存在")
        old_lines = c.execute("SELECT * FROM purchase_lines WHERE order_id=?", (oid,)).fetchall()
        # 把关1: 本单批次必须完整未动(未出库消耗、未调拨、余额全在原仓且等于初始量)
        for ln in old_lines:
            bid = ln["batch_id"]
            consumed = c.execute(
                "SELECT COUNT(*) FROM sales_allocations WHERE batch_id=?", (bid,)).fetchone()[0]
            moved = c.execute(
                "SELECT COUNT(*) FROM transfer_lines WHERE batch_id=?", (bid,)).fetchone()[0]
            bal = c.execute(
                "SELECT warehouse_id, qty FROM inventory_balances WHERE batch_id=?", (bid,)).fetchall()
            if consumed or moved or len(bal) != 1 or bal[0]["warehouse_id"] != o["warehouse_id"] \
                    or bal[0]["qty"] != ln["qty"]:
                raise HTTPException(400, "批次已被消耗或已调拨,不可改,请取消冲销后重录")
        # 把关2: 应付与付款状态
        pay = c.execute("SELECT * FROM payables WHERE purchase_order_id=?", (oid,)).fetchone()
        paid = pay["paid_cents"] if pay else 0
        if pay and new_total < paid:
            raise HTTPException(400, "新应付总额小于已付金额,请先处理付款记录")
        if pay and body.pay_status == "未付款" and paid > 0:
            raise HTTPException(400, "已有付款记录,不能改回未付款")
        # 把关3: 改后各商品在新仓的库存不得小于已预定数量
        delta = {}
        for ln in old_lines:
            k = (ln["product_id"], o["warehouse_id"])
            delta[k] = delta.get(k, 0) - ln["qty"]
        for pid, qty, *_ in lines:
            k = (pid, body.warehouse_id)
            delta[k] = delta.get(k, 0) + qty
        for (pid, wid), d in delta.items():
            if stock_qty(c, pid, wid) + d < reserved_qty(c, pid, wid):
                raise HTTPException(400, "改后库存小于已预定数量,不可改")
        before = {"order": dict(o), "lines": [dict(x) for x in old_lines],
                  "payable": dict(pay) if pay else None}
        # 冲销旧批次库存并重建(流水留痕)
        for ln in old_lines:
            c.execute("DELETE FROM inventory_balances WHERE batch_id=?", (ln["batch_id"],))
            c.execute(
                "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("入库修改冲销", ln["product_id"], ln["batch_id"], o["warehouse_id"], -ln["qty"],
                 "purchase_order", oid, o["order_no"], now_iso()))
            c.execute("DELETE FROM batches WHERE id=?", (ln["batch_id"],))
        c.execute("DELETE FROM purchase_lines WHERE order_id=?", (oid,))
        for (pid, qty, up, lt), share in zip(lines, shares):
            bcur = c.execute(
                "INSERT INTO batches(product_id,supplier_id,purchase_line_id,qty_initial,"
                "unit_price_cents,total_cost_cents,biz_date,created_at) VALUES(?,?,NULL,?,?,?,?,?)",
                (pid, body.supplier_id, qty, up, lt + share, body.biz_date, now_iso()))
            bid = bcur.lastrowid
            lcur = c.execute(
                "INSERT INTO purchase_lines(order_id,product_id,qty,unit_price_cents,"
                "line_total_cents,freight_share_cents,batch_id) VALUES(?,?,?,?,?,?,?)",
                (oid, pid, qty, up, lt, share, bid))
            c.execute("UPDATE batches SET purchase_line_id=? WHERE id=?", (lcur.lastrowid, bid))
            c.execute(
                "INSERT INTO inventory_balances(batch_id,warehouse_id,qty) VALUES(?,?,?)",
                (bid, body.warehouse_id, qty))
            c.execute(
                "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("入库修改", pid, bid, body.warehouse_id, qty,
                 "purchase_order", oid, o["order_no"], now_iso()))
        # 应付重算; 改为已付款时把差额记一笔付款核销
        new_pay_status = o["pay_status"] or "未付款"
        if pay:
            c.execute("UPDATE payables SET supplier_id=?,amount_cents=?,biz_date=? WHERE id=?",
                      (body.supplier_id, new_total, body.biz_date, pay["id"]))
            if body.pay_status == "已付款" and new_total > paid:
                c.execute(
                    "INSERT INTO payments(payable_id,supplier_id,amount_cents,pay_date,note,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (pay["id"], body.supplier_id, new_total - paid, body.biz_date,
                     "入库修改时已付款", now_iso()))
                c.execute("UPDATE payables SET paid_cents=? WHERE id=?", (new_total, pay["id"]))
                new_pay_status = "已付款"
            elif body.pay_status == "未付款":
                new_pay_status = "未付款"
        c.execute(
            "UPDATE purchase_orders SET supplier_id=?,warehouse_id=?,biz_date=?,freight_cents=?,"
            "goods_cents=?,total_cents=?,note=?,pay_status=? WHERE id=?",
            (body.supplier_id, body.warehouse_id, body.biz_date, freight, goods, new_total,
             body.note.strip(), new_pay_status, oid))
        after_o = c.execute("SELECT * FROM purchase_orders WHERE id=?", (oid,)).fetchone()
        after = {"order": dict(after_o),
                 "lines": [dict(x) for x in c.execute(
                     "SELECT * FROM purchase_lines WHERE order_id=?", (oid,))],
                 "payable": dict(c.execute(
                     "SELECT * FROM payables WHERE purchase_order_id=?", (oid,)).fetchone())
                 if pay else None}
        log_edit(c, "purchase_order", oid, before, after, u["username"])
    return {"ok": True, "id": oid}


# ---------------------------------------------------------------- 库存
@app.get("/api/inventory")
def inventory(view: str = "product", wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    with db() as c:
        if view == "warehouse":
            rows = c.execute(
                "SELECT bal.warehouse_id, b.product_id, w.name warehouse_name, p.name product_name, p.type, p.vintage, "
                "SUM(bal.qty) qty FROM inventory_balances bal "
                "JOIN batches b ON b.id=bal.batch_id JOIN products p ON p.id=b.product_id "
                "JOIN warehouses w ON w.id=bal.warehouse_id WHERE bal.qty<>0 "
                "GROUP BY bal.warehouse_id, b.product_id ORDER BY w.name, p.name"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["reserved_qty"] = reserved_qty(c, d["product_id"], d["warehouse_id"])
                d["available_qty"] = d["qty"] - d["reserved_qty"]
                out.append(d)
            return out
        if view == "batch":
            rows = c.execute(
                "SELECT b.id batch_id, p.name product_name, p.type, p.vintage, p.origin, "
                "s.name supplier_name, b.biz_date, b.qty_initial, b.unit_price_cents, "
                "b.total_cost_cents, COALESCE(SUM(bal.qty),0) qty_remaining, "
                "GROUP_CONCAT(w.name||':'||bal.qty) locations "
                "FROM batches b JOIN products p ON p.id=b.product_id "
                "LEFT JOIN suppliers s ON s.id=b.supplier_id "
                "LEFT JOIN inventory_balances bal ON bal.batch_id=b.id "
                "LEFT JOIN warehouses w ON w.id=bal.warehouse_id "
                "GROUP BY b.id ORDER BY b.id DESC"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["unit_cost"] = round(d["total_cost_cents"] / d["qty_initial"] / 100, 4) if d["qty_initial"] else 0
                out.append(d)
            return out
        rows = c.execute(
            "SELECT p.id product_id, p.name product_name, p.type, p.category, p.vintage, p.origin, "
            "COALESCE(SUM(bal.qty),0) qty, "
            "COALESCE(SUM(bal.qty * b.total_cost_cents / b.qty_initial),0) value_cents "
            "FROM products p LEFT JOIN batches b ON b.product_id=p.id "
            "LEFT JOIN inventory_balances bal ON bal.batch_id=b.id "
            "GROUP BY p.id ORDER BY p.type, p.name"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reserved_qty"] = reserved_qty(c, d["product_id"])
            d["available_qty"] = d["qty"] - d["reserved_qty"]
            out.append(d)
        return out


@app.get("/api/inventory-txns")
def inventory_txns(wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    with db() as c:
        rows = c.execute(
            "SELECT t.*, p.name product_name, w.name warehouse_name FROM inventory_txns t "
            "JOIN products p ON p.id=t.product_id "
            "LEFT JOIN warehouses w ON w.id=t.warehouse_id ORDER BY t.id DESC LIMIT 200"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- 调拨
@app.post("/api/transfers")
def create_transfer(body: TransferIn, wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    if body.from_warehouse_id == body.to_warehouse_id:
        raise HTTPException(400, "调出调入不能是同一仓库")
    if not body.lines:
        raise HTTPException(400, "至少一行明细")
    with db() as c:
        if body.idempotency_key:
            old = c.execute(
                "SELECT id,transfer_no FROM transfers WHERE idempotency_key=?",
                (body.idempotency_key,)).fetchone()
            if old:
                return {"id": old["id"], "transfer_no": old["transfer_no"], "duplicated": True}
        # 先校验全部行库存足够(事务内原子扣减); 已被货单预定的可用库存不能调走
        want_by_product = {}
        for ln in body.lines:
            if ln.qty <= 0:
                raise HTTPException(400, "数量必须大于0")
            bal = c.execute(
                "SELECT qty FROM inventory_balances WHERE batch_id=? AND warehouse_id=?",
                (ln.batch_id, body.from_warehouse_id)).fetchone()
            if not bal or bal["qty"] < ln.qty:
                raise HTTPException(400, "调出仓库存不足")
            brow = c.execute("SELECT product_id FROM batches WHERE id=?", (ln.batch_id,)).fetchone()
            if not brow:
                raise HTTPException(400, "批次不存在")
            want_by_product[brow["product_id"]] = want_by_product.get(brow["product_id"], 0) + ln.qty
        for pid, want in want_by_product.items():
            avail = stock_qty(c, pid, body.from_warehouse_id) - reserved_qty(c, pid, body.from_warehouse_id)
            if want > avail:
                raise HTTPException(400, "可用库存不足(部分已被货单预定)")
        no = next_no(c, "DB", "transfers", "transfer_no")
        cur = c.execute(
            "INSERT INTO transfers(transfer_no,from_warehouse_id,to_warehouse_id,biz_date,"
            "status,idempotency_key,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (no, body.from_warehouse_id, body.to_warehouse_id, body.biz_date, "已确认",
             body.idempotency_key or None, body.note.strip(), now_iso()),
        )
        tid = cur.lastrowid
        for ln in body.lines:
            batch = c.execute("SELECT product_id FROM batches WHERE id=?", (ln.batch_id,)).fetchone()
            pid = batch["product_id"]
            c.execute(
                "UPDATE inventory_balances SET qty=qty-? WHERE batch_id=? AND warehouse_id=? AND qty>=?",
                (ln.qty, ln.batch_id, body.from_warehouse_id, ln.qty))
            c.execute(
                "INSERT INTO inventory_balances(batch_id,warehouse_id,qty) VALUES(?,?,?) "
                "ON CONFLICT(batch_id,warehouse_id) DO UPDATE SET qty=qty+excluded.qty",
                (ln.batch_id, body.to_warehouse_id, ln.qty))
            c.execute(
                "INSERT INTO transfer_lines(transfer_id,batch_id,product_id,qty) VALUES(?,?,?,?)",
                (tid, ln.batch_id, pid, ln.qty))
            for ttype, wid, delta in (("调拨出", body.from_warehouse_id, -ln.qty),
                                      ("调拨入", body.to_warehouse_id, ln.qty)):
                c.execute(
                    "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                    "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (ttype, pid, ln.batch_id, wid, delta, "transfer", tid, no, now_iso()))
    return {"id": tid, "transfer_no": no, "duplicated": False}


@app.get("/api/transfers")
def list_transfers(wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    with db() as c:
        rows = c.execute(
            "SELECT t.*, fw.name from_name, tw.name to_name FROM transfers t "
            "JOIN warehouses fw ON fw.id=t.from_warehouse_id "
            "JOIN warehouses tw ON tw.id=t.to_warehouse_id ORDER BY t.id DESC LIMIT 100"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["lines"] = [dict(x) for x in c.execute(
                "SELECT l.*, p.name product_name FROM transfer_lines l "
                "JOIN products p ON p.id=l.product_id WHERE l.transfer_id=?", (d["id"],))]
            out.append(d)
        return out


@app.put("/api/transfers/{tid}")
def edit_transfer(tid: int, body: TransferEditIn, wine_token: Optional[str] = Cookie(default=None)):
    """改调拨单(数量/目标仓/日期/备注; 调出仓不变): 冲销原调拨流水后重记。

    把关: 目标仓当前库存不足以冲销原调拨时拒绝; 重记时调出仓可用量校验同新建。
    """
    u = require_user(wine_token)
    if not body.lines:
        raise HTTPException(400, "至少一行明细")
    with db() as c:
        t = c.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()
        if not t:
            raise HTTPException(404, "调拨单不存在")
        from_wh = t["from_warehouse_id"]
        if body.to_warehouse_id == from_wh:
            raise HTTPException(400, "调出调入不能是同一仓库")
        if not c.execute("SELECT id FROM warehouses WHERE id=?", (body.to_warehouse_id,)).fetchone():
            raise HTTPException(400, "调入仓不存在")
        old_lines = c.execute("SELECT * FROM transfer_lines WHERE transfer_id=?", (tid,)).fetchall()
        # 把关1: 冲销可行性——原调入仓里该批次现存量必须够冲回
        need_back = {}
        for ln in old_lines:
            need_back[ln["batch_id"]] = need_back.get(ln["batch_id"], 0) + ln["qty"]
        for bid, q in need_back.items():
            bal = c.execute(
                "SELECT qty FROM inventory_balances WHERE batch_id=? AND warehouse_id=?",
                (bid, t["to_warehouse_id"])).fetchone()
            if not bal or bal["qty"] < q:
                raise HTTPException(400, "目标仓库存不足以冲销原调拨,不可改")
        # 把关2: 新明细校验(按冲销后的调出仓余额算)
        new_lines = []
        want_batch = {}
        want_product = {}
        for ln in body.lines:
            if ln.qty <= 0:
                raise HTTPException(400, "数量必须大于0")
            brow = c.execute("SELECT product_id FROM batches WHERE id=?", (ln.batch_id,)).fetchone()
            if not brow:
                raise HTTPException(400, "批次不存在")
            new_lines.append((ln.batch_id, brow["product_id"], ln.qty))
            want_batch[ln.batch_id] = want_batch.get(ln.batch_id, 0) + ln.qty
            want_product[brow["product_id"]] = want_product.get(brow["product_id"], 0) + ln.qty
        for bid, q in want_batch.items():
            bal = c.execute(
                "SELECT qty FROM inventory_balances WHERE batch_id=? AND warehouse_id=?",
                (bid, from_wh)).fetchone()
            have = (bal["qty"] if bal else 0) + need_back.get(bid, 0)
            if q > have:
                raise HTTPException(400, "调出仓库存不足")
        for pid, q in want_product.items():
            back = sum(ln["qty"] for ln in old_lines if ln["product_id"] == pid)
            avail = stock_qty(c, pid, from_wh) + back - reserved_qty(c, pid, from_wh)
            if q > avail:
                raise HTTPException(400, "可用库存不足(部分已被货单预定)")
        before = {"transfer": dict(t), "lines": [dict(x) for x in old_lines]}
        # 冲销原调拨
        for ln in old_lines:
            c.execute(
                "UPDATE inventory_balances SET qty=qty-? WHERE batch_id=? AND warehouse_id=? AND qty>=?",
                (ln["qty"], ln["batch_id"], t["to_warehouse_id"], ln["qty"]))
            c.execute(
                "INSERT INTO inventory_balances(batch_id,warehouse_id,qty) VALUES(?,?,?) "
                "ON CONFLICT(batch_id,warehouse_id) DO UPDATE SET qty=qty+excluded.qty",
                (ln["batch_id"], from_wh, ln["qty"]))
            for ttype, wid, delta in (("调拨修改冲销出", t["to_warehouse_id"], -ln["qty"]),
                                      ("调拨修改冲销入", from_wh, ln["qty"])):
                c.execute(
                    "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                    "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (ttype, ln["product_id"], ln["batch_id"], wid, delta,
                     "transfer", tid, t["transfer_no"], now_iso()))
        c.execute("DELETE FROM transfer_lines WHERE transfer_id=?", (tid,))
        # 重记新调拨
        for bid, pid, qty in new_lines:
            c.execute(
                "UPDATE inventory_balances SET qty=qty-? WHERE batch_id=? AND warehouse_id=? AND qty>=?",
                (qty, bid, from_wh, qty))
            c.execute(
                "INSERT INTO inventory_balances(batch_id,warehouse_id,qty) VALUES(?,?,?) "
                "ON CONFLICT(batch_id,warehouse_id) DO UPDATE SET qty=qty+excluded.qty",
                (bid, body.to_warehouse_id, qty))
            c.execute(
                "INSERT INTO transfer_lines(transfer_id,batch_id,product_id,qty) VALUES(?,?,?,?)",
                (tid, bid, pid, qty))
            for ttype, wid, delta in (("调拨修改出", from_wh, -qty),
                                      ("调拨修改入", body.to_warehouse_id, qty)):
                c.execute(
                    "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                    "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (ttype, pid, bid, wid, delta, "transfer", tid, t["transfer_no"], now_iso()))
        c.execute("UPDATE transfers SET to_warehouse_id=?,biz_date=?,note=? WHERE id=?",
                  (body.to_warehouse_id, body.biz_date, body.note.strip(), tid))
        after = {"transfer": dict(c.execute("SELECT * FROM transfers WHERE id=?", (tid,)).fetchone()),
                 "lines": [dict(x) for x in c.execute(
                     "SELECT * FROM transfer_lines WHERE transfer_id=?", (tid,))]}
        log_edit(c, "transfer", tid, before, after, u["username"])
    return {"ok": True, "id": tid}


# ---------------------------------------------------------------- 客户货单
def _sales_order_dict(c, o):
    d = dict(o)
    d["lines"] = [dict(x) for x in c.execute(
        "SELECT l.*, p.name product_name, p.type product_type, p.vintage, p.origin "
        "FROM sales_lines l JOIN products p ON p.id=l.product_id WHERE l.order_id=? ORDER BY l.id",
        (d["id"],))]
    d["allocations"] = [dict(x) for x in c.execute(
        "SELECT * FROM sales_allocations WHERE order_id=? ORDER BY id", (d["id"],))]
    d["events"] = [dict(x) for x in c.execute(
        "SELECT * FROM sales_order_events WHERE order_id=? ORDER BY id", (d["id"],))]
    d["cost_cents"] = sum(a["cost_cents"] for a in d["allocations"])
    d["public_path"] = f"/o/{d['public_token']}" if d.get("public_token") else ""
    return d


def _sales_event(c, oid, event, detail=""):
    c.execute("INSERT INTO sales_order_events(order_id,event,detail,created_at) VALUES(?,?,?,?)",
              (oid, event, detail, now_iso()))


@app.post("/api/sales-orders")
def create_sales(body: SalesIn, wine_token: Optional[str] = Cookie(default=None)):
    u = require_user(wine_token)
    if not body.lines:
        raise HTTPException(400, "至少一行明细")
    if body.freight_payer not in ("客户付", "自提无", "我方代付"):
        raise HTTPException(400, "运费承担方不对")
    if body.pay_status not in ("已付款", "未付款"):
        raise HTTPException(400, "付款状态不对")
    freight = to_cents(body.freight or "0")
    if body.freight_payer == "自提无":
        freight = 0
    lines = []
    for ln in body.lines:
        if ln.qty <= 0:
            raise HTTPException(400, "数量必须大于0")
        if ln.line_type not in ("销售", "赠送"):
            raise HTTPException(400, "行类型不对")
        up = 0 if ln.line_type == "赠送" else to_cents(ln.unit_price or "0")
        lines.append((ln.product_id, ln.qty, up, ln.line_type, up * ln.qty))
    goods = sum(x[4] for x in lines)
    total = goods + (freight if body.freight_payer == "客户付" else 0)
    with db() as c:
        if body.idempotency_key:
            old = c.execute("SELECT id,order_no FROM sales_orders WHERE idempotency_key=?",
                            (body.idempotency_key,)).fetchone()
            if old:
                return {"id": old["id"], "order_no": old["order_no"], "duplicated": True}
        if not c.execute("SELECT id FROM customers WHERE id=?", (body.customer_id,)).fetchone():
            raise HTTPException(400, "客户不存在")
        if not c.execute("SELECT id FROM warehouses WHERE id=?", (body.warehouse_id,)).fetchone():
            raise HTTPException(400, "仓库不存在")
        want = {}
        for pid, qty, *_ in lines:
            if not c.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone():
                raise HTTPException(400, "商品不存在")
            want[pid] = want.get(pid, 0) + qty
        for pid, q in want.items():
            avail = stock_qty(c, pid, body.warehouse_id) - reserved_qty(c, pid, body.warehouse_id)
            if q > avail:
                raise HTTPException(400, "可用库存不足,无法预定")
        order_no = next_no(c, "HD", "sales_orders", "order_no")
        token = secrets.token_urlsafe(24)
        pay_time = now_iso() if body.pay_status == "已付款" else None
        cur = c.execute(
            "INSERT INTO sales_orders(order_no,customer_id,warehouse_id,biz_date,freight_cents,"
            "freight_payer,goods_cents,total_cents,order_status,ship_status,pay_status,pay_time,"
            "logistics_no,public_token,idempotency_key,note,created_by,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_no, body.customer_id, body.warehouse_id, body.biz_date or now_iso()[:10],
             freight, body.freight_payer, goods, total, "正常", "未发货", body.pay_status,
             pay_time, body.logistics_no.strip(), token, body.idempotency_key or None,
             body.note.strip(), u["username"], now_iso()))
        oid = cur.lastrowid
        for pid, qty, up, ltype, lt in lines:
            c.execute(
                "INSERT INTO sales_lines(order_id,product_id,qty,unit_price_cents,line_type,line_total_cents)"
                " VALUES(?,?,?,?,?,?)", (oid, pid, qty, up, ltype, lt))
            c.execute(
                "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("预定", pid, None, body.warehouse_id, 0, "sales_order", oid, order_no, now_iso()))
        _sales_event(c, oid, "建单", f"付款状态:{body.pay_status}")
    return {"id": oid, "order_no": order_no, "public_path": f"/o/{token}", "duplicated": False}


@app.get("/api/sales-orders")
def list_sales(wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    with db() as c:
        rows = c.execute(
            "SELECT o.*, cu.name customer_name, w.name warehouse_name FROM sales_orders o "
            "JOIN customers cu ON cu.id=o.customer_id "
            "JOIN warehouses w ON w.id=o.warehouse_id ORDER BY o.id DESC LIMIT 200").fetchall()
        return [_sales_order_dict(c, r) for r in rows]


@app.post("/api/sales-orders/{oid}/ship")
def ship_sales(oid: int, body: ShipIn, wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    with db() as c:
        o = c.execute("SELECT * FROM sales_orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise HTTPException(404, "货单不存在")
        if o["order_status"] == "已取消":
            raise HTTPException(400, "已取消的单不能发货")
        if o["ship_status"] == "已发货":
            raise HTTPException(400, "已发货,不能重复发货")
        lines = c.execute("SELECT * FROM sales_lines WHERE order_id=? ORDER BY id", (oid,)).fetchall()
        # FIFO: 按批次 biz_date, id 先后扣减该仓库存, 记录实际批次成本
        for ln in lines:
            need = ln["qty"]
            batches = c.execute(
                "SELECT b.id batch_id, b.total_cost_cents, b.qty_initial, bal.qty bal_qty "
                "FROM batches b JOIN inventory_balances bal ON bal.batch_id=b.id "
                "WHERE b.product_id=? AND bal.warehouse_id=? AND bal.qty>0 "
                "ORDER BY b.biz_date, b.id", (ln["product_id"], o["warehouse_id"])).fetchall()
            if sum(b["bal_qty"] for b in batches) < need:
                raise HTTPException(400, "库存不足,无法发货")
            for b in batches:
                if need <= 0:
                    break
                take = min(need, b["bal_qty"])
                cost = int((Decimal(take * b["total_cost_cents"]) / Decimal(b["qty_initial"]))
                           .quantize(Decimal("1"), rounding=ROUND_HALF_UP)) if b["qty_initial"] else 0
                c.execute(
                    "UPDATE inventory_balances SET qty=qty-? WHERE batch_id=? AND warehouse_id=? AND qty>=?",
                    (take, b["batch_id"], o["warehouse_id"], take))
                c.execute(
                    "INSERT INTO sales_allocations(order_id,line_id,batch_id,qty,cost_cents,created_at)"
                    " VALUES(?,?,?,?,?,?)", (oid, ln["id"], b["batch_id"], take, cost, now_iso()))
                c.execute(
                    "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                    "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("赠送出库" if ln["line_type"] == "赠送" else "销售出库",
                     ln["product_id"], b["batch_id"], o["warehouse_id"], -take,
                     "sales_order", oid, o["order_no"], now_iso()))
                need -= take
        ship_time = body.ship_time.strip() or now_iso()
        c.execute("UPDATE sales_orders SET ship_status='已发货', ship_time=?, logistics_no=? WHERE id=?",
                  (ship_time, body.logistics_no.strip() or o["logistics_no"], oid))
        _sales_event(c, oid, "发货", f"物流号:{body.logistics_no.strip() or o['logistics_no']}")
    return {"ok": True}


@app.post("/api/sales-orders/{oid}/cancel")
def cancel_sales(oid: int, body: CancelIn, wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    with db() as c:
        o = c.execute("SELECT * FROM sales_orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise HTTPException(404, "货单不存在")
        if o["order_status"] == "已取消":
            raise HTTPException(400, "已取消")
        if o["ship_status"] == "已发货":
            raise HTTPException(400, "已发货的单不可取消,请走退货(二期)")
        c.execute("UPDATE sales_orders SET order_status='已取消', cancelled_at=?, cancel_reason=? WHERE id=?",
                  (now_iso(), body.reason.strip(), oid))
        for ln in c.execute("SELECT * FROM sales_lines WHERE order_id=?", (oid,)).fetchall():
            c.execute(
                "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("释放预定", ln["product_id"], None, o["warehouse_id"], 0,
                 "sales_order", oid, o["order_no"], now_iso()))
        _sales_event(c, oid, "取消", body.reason.strip())
    return {"ok": True}


@app.post("/api/sales-orders/{oid}/pay-status")
def set_pay_status(oid: int, body: PayStatusIn, wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    if body.pay_status not in ("已付款", "未付款"):
        raise HTTPException(400, "付款状态不对")
    with db() as c:
        o = c.execute("SELECT * FROM sales_orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise HTTPException(404, "货单不存在")
        if o["order_status"] == "已取消":
            raise HTTPException(400, "已取消的单不能改付款状态")
        pay_time = now_iso() if body.pay_status == "已付款" else None
        c.execute("UPDATE sales_orders SET pay_status=?, pay_time=? WHERE id=?",
                  (body.pay_status, pay_time, oid))
        _sales_event(c, oid, "付款状态", f"{o['pay_status']}->{body.pay_status}")
    return {"ok": True}


@app.put("/api/sales-orders/{oid}")
def edit_sales(oid: int, body: SalesEditIn, wine_token: Optional[str] = Cookie(default=None)):
    """改客户货单: 未发货可全改(预定重算, 超可用拒绝); 已发货只可改物流号/备注。"""
    u = require_user(wine_token)
    if not body.lines:
        raise HTTPException(400, "至少一行明细")
    if body.freight_payer not in ("客户付", "自提无", "我方代付"):
        raise HTTPException(400, "运费承担方不对")
    freight = to_cents(body.freight or "0")
    if body.freight_payer == "自提无":
        freight = 0
    lines = []
    for ln in body.lines:
        if ln.qty <= 0:
            raise HTTPException(400, "数量必须大于0")
        if ln.line_type not in ("销售", "赠送"):
            raise HTTPException(400, "行类型不对")
        up = 0 if ln.line_type == "赠送" else to_cents(ln.unit_price or "0")
        lines.append((ln.product_id, ln.qty, up, ln.line_type, up * ln.qty))
    goods = sum(x[4] for x in lines)
    total = goods + (freight if body.freight_payer == "客户付" else 0)
    with db() as c:
        o = c.execute("SELECT * FROM sales_orders WHERE id=?", (oid,)).fetchone()
        if not o:
            raise HTTPException(404, "货单不存在")
        if o["order_status"] == "已取消":
            raise HTTPException(400, "已取消的单不可改")
        old_lines = c.execute(
            "SELECT * FROM sales_lines WHERE order_id=? ORDER BY id", (oid,)).fetchall()
        before = _sales_order_dict(c, o)
        if o["ship_status"] == "已发货":
            # 已发货: 除物流号/备注外任何字段变化都拒绝
            old_key = sorted((x["product_id"], x["qty"], x["unit_price_cents"], x["line_type"])
                             for x in old_lines)
            new_key = sorted((p, q, up, t) for p, q, up, t, _ in lines)
            if (body.customer_id != o["customer_id"] or body.warehouse_id != o["warehouse_id"]
                    or freight != o["freight_cents"] or body.freight_payer != o["freight_payer"]
                    or new_key != old_key):
                raise HTTPException(400, "已发货货单只能改物流号和备注,明细不可改")
            c.execute("UPDATE sales_orders SET logistics_no=?,note=? WHERE id=?",
                      (body.logistics_no.strip(), body.note.strip(), oid))
            _sales_event(c, oid, "修改", "已发货单改物流号/备注")
            after = _sales_order_dict(
                c, c.execute("SELECT * FROM sales_orders WHERE id=?", (oid,)).fetchone())
            log_edit(c, "sales_order", oid, before, after, u["username"])
            return {"ok": True, "id": oid}
        # 未发货: 全改, 预定按新明细重算(可用量扣除本单原预定后再校验)
        if not c.execute("SELECT id FROM customers WHERE id=?", (body.customer_id,)).fetchone():
            raise HTTPException(400, "客户不存在")
        if not c.execute("SELECT id FROM warehouses WHERE id=?", (body.warehouse_id,)).fetchone():
            raise HTTPException(400, "仓库不存在")
        own_old = {}
        for x in old_lines:
            own_old[x["product_id"]] = own_old.get(x["product_id"], 0) + x["qty"]
        want = {}
        for pid, qty, *_ in lines:
            if not c.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone():
                raise HTTPException(400, "商品不存在")
            want[pid] = want.get(pid, 0) + qty
        for pid, q in want.items():
            reserved_excl = reserved_qty(c, pid, body.warehouse_id)
            if body.warehouse_id == o["warehouse_id"]:
                reserved_excl -= own_old.get(pid, 0)
            avail = stock_qty(c, pid, body.warehouse_id) - reserved_excl
            if q > avail:
                raise HTTPException(400, "可用库存不足,无法预定")
        c.execute(
            "UPDATE sales_orders SET customer_id=?,warehouse_id=?,biz_date=?,freight_cents=?,"
            "freight_payer=?,goods_cents=?,total_cents=?,logistics_no=?,note=? WHERE id=?",
            (body.customer_id, body.warehouse_id, body.biz_date or o["biz_date"], freight,
             body.freight_payer, goods, total, body.logistics_no.strip(), body.note.strip(), oid))
        c.execute("DELETE FROM sales_lines WHERE order_id=?", (oid,))
        for pid, qty, up, ltype, lt in lines:
            c.execute(
                "INSERT INTO sales_lines(order_id,product_id,qty,unit_price_cents,line_type,line_total_cents)"
                " VALUES(?,?,?,?,?,?)", (oid, pid, qty, up, ltype, lt))
            c.execute(
                "INSERT INTO inventory_txns(txn_type,product_id,batch_id,warehouse_id,qty_delta,"
                "ref_type,ref_id,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("修改预定", pid, None, body.warehouse_id, 0,
                 "sales_order", oid, o["order_no"], now_iso()))
        _sales_event(c, oid, "修改", "未发货单改明细/运费/客户")
        after = _sales_order_dict(
            c, c.execute("SELECT * FROM sales_orders WHERE id=?", (oid,)).fetchone())
        log_edit(c, "sales_order", oid, before, after, u["username"])
    return {"ok": True, "id": oid}


# ---------------------------------------------------------------- 公开单页(免登录)
def _public_order(c, token):
    o = c.execute(
        "SELECT o.*, cu.name customer_name, cu.phone customer_phone, cu.address customer_address FROM sales_orders o "
        "JOIN customers cu ON cu.id=o.customer_id WHERE o.public_token=?", (token,)).fetchone()
    if not o:
        return None
    d = dict(o)
    d["lines"] = [dict(x) for x in c.execute(
        "SELECT l.qty, l.unit_price_cents, l.line_type, l.line_total_cents, "
        "p.name product_name, p.type product_type, p.vintage, p.origin "
        "FROM sales_lines l JOIN products p ON p.id=l.product_id WHERE l.order_id=? ORDER BY l.id",
        (d["id"],))]
    return d


@app.get("/api/public/orders/{token}")
def public_order_json(token: str):
    with db() as c:
        d = _public_order(c, token)
    if not d:
        raise HTTPException(404, "单子不存在")
    return {k: d[k] for k in ("order_no", "customer_name", "customer_phone", "customer_address", "biz_date", "freight_cents",
            "freight_payer", "goods_cents", "total_cents", "order_status", "ship_status",
            "pay_status", "pay_time", "ship_time", "logistics_no", "note", "lines")}


@app.get("/o/{token}")
def public_order_page(token: str):
    from fastapi.responses import HTMLResponse
    with db() as c:
        d = _public_order(c, token)
    if not d:
        return HTMLResponse("<h3>单子不存在</h3>", status_code=404)

    def esc(s):
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    def m(cents):
        return f"¥{cents / 100:.2f}"

    rows = "".join(
        f"<tr><td>{esc(l['product_name'])}{(' ' + esc(l['vintage'])) if l['vintage'] else ''}"
        f"<div class='s'>{esc(l['product_type'])}{(' · ' + esc(l['origin'])) if l['origin'] else ''}"
        f"{' · 赠送' if l['line_type'] == '赠送' else ''}</div></td>"
        f"<td class='n'>{l['qty']}</td><td class='n'>{m(l['unit_price_cents'])}</td>"
        f"<td class='n'>{m(l['line_total_cents'])}</td></tr>" for l in d["lines"])
    status = "已取消" if d["order_status"] == "已取消" else f"{d['ship_status']} · {d['pay_status']}"
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>货单 {esc(d['order_no'])}</title>
<style>*{{box-sizing:border-box}}body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f6f7;margin:0;color:#1c1c1e}}
.w{{max-width:640px;margin:0 auto;padding:14px 12px 40px}}.c{{background:#fff;border:1px solid #e4e4e8;border-radius:12px;padding:14px}}
h1{{font-size:18px;margin:0 0 4px}}.s{{font-size:12px;color:#777}}table{{width:100%;border-collapse:collapse;font-size:14px;margin-top:10px}}
td,th{{padding:7px 4px;border-bottom:1px solid #f0f0f2;text-align:left}}.n{{text-align:right}}
.r{{display:flex;justify-content:space-between;padding:5px 0;font-size:14px}}.tot{{font-size:17px;font-weight:700;color:#7a1f2b}}</style></head>
<body><div class="w"><div class="c"><h1>货单 {esc(d['order_no'])}</h1>
<div class="s">客户:{esc(d['customer_name'])}{(' · ' + esc(d['customer_phone'])) if d['customer_phone'] else ''} · 状态:{esc(status)}</div>
{f"<div class='s'>地址:{esc(d['customer_address'])}</div>" if d['customer_address'] else ""}
<table><tr><th>商品</th><th class="n">数量</th><th class="n">单价</th><th class="n">小计</th></tr>{rows}</table>
<div class="r"><span>货款</span><span>{m(d['goods_cents'])}</span></div>
<div class="r"><span>运费({esc(d['freight_payer'])})</span><span>{m(d['freight_cents'])}</span></div>
<div class="r tot"><span>总计费用</span><span>{m(d['total_cents'])}</span></div>
<div class="r"><span>物流号</span><span>{esc(d['logistics_no'] or '—')}</span></div>
<div class="r"><span>发货时间</span><span>{esc((d['ship_time'] or '—').replace('T', ' ')[:16])}</span></div>
{f"<div class='s'>备注:{esc(d['note'])}</div>" if d['note'] else ""}
</div></div></body></html>"""
    return HTMLResponse(html)


# ---------------------------------------------------------------- 应付/付款
@app.get("/api/payables")
def list_payables(wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    with db() as c:
        rows = c.execute(
            "SELECT py.*, s.name supplier_name, o.order_no FROM payables py "
            "JOIN suppliers s ON s.id=py.supplier_id "
            "JOIN purchase_orders o ON o.id=py.purchase_order_id ORDER BY py.id DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["balance_cents"] = d["amount_cents"] - d["paid_cents"]
            d["payments"] = [dict(x) for x in c.execute(
                "SELECT * FROM payments WHERE payable_id=? ORDER BY id", (d["id"],))]
            out.append(d)
        return out


@app.post("/api/payables/{pid}/payments")
def add_payment(pid: int, body: PaymentIn, wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    amt = to_cents(body.amount)
    if amt <= 0:
        raise HTTPException(400, "付款金额必须大于0")
    with db() as c:
        pay = c.execute("SELECT * FROM payables WHERE id=?", (pid,)).fetchone()
        if not pay:
            raise HTTPException(404, "应付不存在")
        if pay["paid_cents"] + amt > pay["amount_cents"]:
            raise HTTPException(400, "付款超过应付余额")
        c.execute(
            "INSERT INTO payments(payable_id,supplier_id,amount_cents,pay_date,note,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (pid, pay["supplier_id"], amt, body.pay_date, body.note.strip(), now_iso()))
        c.execute("UPDATE payables SET paid_cents=paid_cents+? WHERE id=?", (amt, pid))
        row = c.execute("SELECT amount_cents,paid_cents FROM payables WHERE id=?", (pid,)).fetchone()
    return {"ok": True, "balance": yuan(row["amount_cents"] - row["paid_cents"])}


@app.put("/api/payments/{payid}")
def edit_payment(payid: int, body: PaymentIn, wine_token: Optional[str] = Cookie(default=None)):
    """改付款记录(金额/日期/备注): 已付总额重算, 不得超过应付总额。"""
    u = require_user(wine_token)
    amt = to_cents(body.amount)
    if amt <= 0:
        raise HTTPException(400, "付款金额必须大于0")
    with db() as c:
        old = c.execute("SELECT * FROM payments WHERE id=?", (payid,)).fetchone()
        if not old:
            raise HTTPException(404, "付款记录不存在")
        pay = c.execute("SELECT * FROM payables WHERE id=?", (old["payable_id"],)).fetchone()
        if not pay:
            raise HTTPException(404, "应付不存在")
        new_paid = pay["paid_cents"] - old["amount_cents"] + amt
        if new_paid > pay["amount_cents"]:
            raise HTTPException(400, "付款总额超过应付总额")
        c.execute("UPDATE payments SET amount_cents=?,pay_date=?,note=? WHERE id=?",
                  (amt, body.pay_date, body.note.strip(), payid))
        c.execute("UPDATE payables SET paid_cents=? WHERE id=?", (new_paid, pay["id"]))
        new = c.execute("SELECT * FROM payments WHERE id=?", (payid,)).fetchone()
        log_edit(c, "payment", payid, dict(old), dict(new), u["username"])
    return {"ok": True, "balance": yuan(pay["amount_cents"] - new_paid)}


# ---------------------------------------------------------------- 修改记录
@app.get("/api/edit-logs")
def list_edit_logs(entity_type: str = "", entity_id: int = 0,
                   wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    with db() as c:
        q = "SELECT * FROM edit_logs WHERE 1=1"
        args = []
        if entity_type:
            q += " AND entity_type=?"
            args.append(entity_type)
        if entity_id:
            q += " AND entity_id=?"
            args.append(entity_id)
        q += " ORDER BY id DESC LIMIT 200"
        return [dict(r) for r in c.execute(q, args)]


# ---------------------------------------------------------------- 备份
@app.post("/api/backup")
def backup(wine_token: Optional[str] = Cookie(default=None)):
    require_user(wine_token)
    bdir = DATA_DIR / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    name = f"wine-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    src = connect()
    dst = sqlite3.connect(bdir / name)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return {"ok": True, "file": name, "path": str(bdir / name)}
