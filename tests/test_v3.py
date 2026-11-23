#!/usr/bin/env python3
"""v0.2.0 自测: 老库升级兼容 + 客户货单/预定/公开单页/付款状态/注册开关。
真实 HTTP。老库阶段需要 v0.1.1 快照 /tmp/wine-old/app.py(缺失时跳过该阶段并提示)。
运行: python3 tests/test_v3.py
"""
import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
OLD_DIR = Path("/tmp/wine-old")
passed = failed = skipped = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


class Client:
    def __init__(self, port, cookies=True):
        self.base = f"http://127.0.0.1:{port}"
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())] if cookies else []
        self.opener = urllib.request.build_opener(*handlers)

    def call(self, method, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, (r.read() if raw else json.loads(r.read() or b"{}"))
        except urllib.error.HTTPError as e:
            body_bytes = e.read() or b"{}"
            return e.code, (body_bytes if raw else json.loads(body_bytes))


def start(cwd, port, data_dir, admin_pw=None):
    env = dict(os.environ, WINE_DATA_DIR=str(data_dir))
    if admin_pw:
        env["WINE_ADMIN_PASSWORD"] = admin_pw
    else:
        env.pop("WINE_ADMIN_PASSWORD", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    cli = Client(port)
    for _ in range(60):
        try:
            cli.call("GET", "/api/me")
            return proc, cli
        except Exception:
            time.sleep(0.3)
    print(proc.stdout.read() if proc.stdout else "")
    raise SystemExit(f"服务没起来 port={port}")


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def main():
    global skipped
    # ---------- 阶段1: v0.1.1 老代码建老库 ----------
    old_data = tempfile.mkdtemp(prefix="wine-old-")
    backup_path = None
    if (OLD_DIR / "app.py").exists():
        print("[1] 老代码 v0.1.1 建老库并录数据")
        proc, c = start(OLD_DIR, 8020, old_data, admin_pw="test-pass-123")
        try:
            s, d = c.call("POST", "/api/login", {"username": "allen314", "password": "test-pass-123"})
            check("老版管理员登录", s == 200, str(d))
            _, sup = c.call("POST", "/api/suppliers", {"name": "老供应商"})
            _, wh = c.call("POST", "/api/warehouses", {"name": "老主仓"})
            _, w1 = c.call("POST", "/api/products", {"type": "酒", "name": "老酒甲", "category": "红葡萄酒", "vintage": "2018", "origin": "法国"})
            _, acc = c.call("POST", "/api/products", {"type": "附件", "name": "礼品袋"})
            s, o = c.call("POST", "/api/purchase-orders", {
                "type": "采购入库", "supplier_id": sup["id"], "warehouse_id": wh["id"],
                "biz_date": "2026-11-20", "freight": "0",
                "lines": [{"product_id": w1["id"], "qty": 12, "unit_price": "100"}],
                "idempotency_key": "old-1"})
            check("老版入库", s == 200, str(o))
            s, bk = c.call("POST", "/api/backup", {})
            backup_path = bk.get("path")
            check("老版备份生成", s == 200 and backup_path and Path(backup_path).exists(), str(bk))
        finally:
            stop(proc)
    else:
        skipped += 1
        print("[1] SKIP 老库阶段: /tmp/wine-old/app.py 不存在")

    # ---------- 阶段2: 新代码打开老库 ----------
    print("[2] 新代码打开老库(升级兼容)")
    proc, c = start(APP_DIR, 8021, old_data, admin_pw="test-pass-123")
    try:
        s, d = c.call("POST", "/api/login", {"username": "allen314", "password": "test-pass-123"})
        check("老管理员升级后仍可登录", s == 200 and d.get("ok"), str(d))
        _, prods = c.call("GET", "/api/products")
        check("老商品完好", any(p["name"] == "老酒甲" for p in prods), str(prods))
        _, inv = c.call("GET", "/api/inventory?view=product")
        old_wine = [r for r in inv if r["product_name"] == "老酒甲"][0]
        check("老库存完好(12件)", old_wine["qty"] == 12, str(old_wine))
        _, pays = c.call("GET", "/api/payables")
        check("老应付完好(¥1200)", len(pays) == 1 and pays[0]["amount_cents"] == 120000, str(pays))
        _, me = c.call("GET", "/api/me")
        check("版本已升 v0.2.0", me.get("version") == "v0.2.0", str(me))

        w1 = [p for p in prods if p["name"] == "老酒甲"][0]
        acc = [p for p in prods if p["name"] == "礼品袋"][0]
        _, sups = c.call("GET", "/api/suppliers")
        sup = sups[0]
        _, whs = c.call("GET", "/api/warehouses")
        wh = whs[0]
        _, cust = c.call("POST", "/api/customers", {"name": "客户张三", "phone": "13800000000"})
        # 期初补礼品袋库存
        c.call("POST", "/api/purchase-orders", {
            "type": "期初库存", "supplier_id": None, "warehouse_id": wh["id"],
            "biz_date": "2026-11-23", "freight": "0",
            "lines": [{"product_id": acc["id"], "qty": 20, "unit_price": "5"}],
            "idempotency_key": "v3-acc"})

        print("[3] 入库付款状态: 已付款/未付款两路")
        s, op = c.call("POST", "/api/purchase-orders", {
            "type": "采购入库", "supplier_id": sup["id"], "warehouse_id": wh["id"],
            "biz_date": "2026-11-23", "freight": "0", "pay_status": "已付款",
            "lines": [{"product_id": w1["id"], "qty": 2, "unit_price": "100"}],
            "idempotency_key": "v3-paid"})
        _, pays = c.call("GET", "/api/payables")
        pp = [p for p in pays if p["order_no"] == op["order_no"]][0]
        check("已付款入库应付余额0且有付款记录",
              pp["balance_cents"] == 0 and len(pp["payments"]) == 1 and pp["payments"][0]["amount_cents"] == 20000, str(pp))
        s, ou = c.call("POST", "/api/purchase-orders", {
            "type": "采购入库", "supplier_id": sup["id"], "warehouse_id": wh["id"],
            "biz_date": "2026-11-23", "freight": "0", "pay_status": "未付款",
            "lines": [{"product_id": w1["id"], "qty": 2, "unit_price": "100"}],
            "idempotency_key": "v3-unpaid"})
        _, pays = c.call("GET", "/api/payables")
        pu = [p for p in pays if p["order_no"] == ou["order_no"]][0]
        check("未付款入库应付余额¥200无付款记录", pu["balance_cents"] == 20000 and len(pu["payments"]) == 0, str(pu))
        # 现在老酒甲总量 16

        print("[4] 建单预定: 可用减少/总量不变/超量拒绝")
        s, so = c.call("POST", "/api/sales-orders", {
            "customer_id": cust["id"], "warehouse_id": wh["id"], "biz_date": "2026-11-23",
            "lines": [{"product_id": w1["id"], "qty": 5, "unit_price": "300", "line_type": "销售"},
                      {"product_id": acc["id"], "qty": 2, "unit_price": "999", "line_type": "赠送"}],
            "freight": "20", "freight_payer": "客户付", "pay_status": "已付款",
            "logistics_no": "", "idempotency_key": "v3-so-1"})
        check("货单创建", s == 200 and so.get("order_no"), str(so))
        _, inv = c.call("GET", "/api/inventory?view=product")
        r = [x for x in inv if x["product_name"] == "老酒甲"][0]
        check("预定后总量16/预定5/可用11", r["qty"] == 16 and r["reserved_qty"] == 5 and r["available_qty"] == 11, str(r))
        s, bad = c.call("POST", "/api/sales-orders", {
            "customer_id": cust["id"], "warehouse_id": wh["id"],
            "lines": [{"product_id": w1["id"], "qty": 12, "unit_price": "300"}],
            "idempotency_key": "v3-so-bad"})
        check("超可用预定被拒", s == 400, f"got {s} {bad}")
        _, orders = c.call("GET", "/api/sales-orders")
        o1 = [o for o in orders if o["id"] == so["id"]][0]
        check("总计=货款1500+客户运费20=1520,赠送行价0",
              o1["total_cents"] == 152000 and o1["goods_cents"] == 150000
              and [l for l in o1["lines"] if l["line_type"] == "赠送"][0]["unit_price_cents"] == 0, str(o1))
        check("发单预付: 已付款且有付款时间", o1["pay_status"] == "已付款" and bool(o1["pay_time"]), str(o1))

        print("[5] 公开单页: 无登录可看且无成本")
        anon = Client(8021, cookies=False)
        s, html = anon.call("GET", so["public_path"], raw=True)
        text = html.decode("utf-8", "ignore")
        check("公开页200且含商品/物流/总计",
              s == 200 and "老酒甲" in text and "礼品袋" in text and "1520" in text, text[:200])
        check("公开页不含成本信息", "成本" not in text and "进价" not in text and "应付" not in text)
        s, pj = anon.call("GET", f"/api/public/orders/{so['public_path'].split('/')[-1]}")
        check("公开JSON无成本字段", s == 200 and "cost" not in json.dumps(pj), str(pj)[:200])

        print("[6] 发货扣库存+批次流水+重复发货拒绝+已发货不可取消")
        s, _ = c.call("POST", f"/api/sales-orders/{so['id']}/ship", {"logistics_no": "SF123456"})
        check("发货成功", s == 200, f"got {s}")
        _, inv = c.call("GET", "/api/inventory?view=product")
        r = [x for x in inv if x["product_name"] == "老酒甲"][0]
        check("发货后总量11/预定0", r["qty"] == 11 and r["reserved_qty"] == 0, str(r))
        _, orders = c.call("GET", "/api/sales-orders")
        o1 = [o for o in orders if o["id"] == so["id"]][0]
        check("发货记批次成本(5×¥100+2×¥5=¥510)且有发货时间/物流号",
              o1["cost_cents"] == 51000 and o1["ship_time"] and o1["logistics_no"] == "SF123456", str(o1))
        _, txns = c.call("GET", "/api/inventory-txns")
        check("有销售出库流水", any(t["txn_type"] == "销售出库" and t["qty_delta"] == -5 for t in txns))
        s, _ = c.call("POST", f"/api/sales-orders/{so['id']}/ship", {"logistics_no": "X"})
        check("重复发货被拒", s == 400, f"got {s}")
        s, _ = c.call("POST", f"/api/sales-orders/{so['id']}/cancel", {"reason": "x"})
        check("已发货不可取消", s == 400, f"got {s}")

        print("[7] 未付单改付款状态留痕 + 取消释放预定且记录保留")
        s, so2 = c.call("POST", "/api/sales-orders", {
            "customer_id": cust["id"], "warehouse_id": wh["id"],
            "lines": [{"product_id": w1["id"], "qty": 3, "unit_price": "300"}],
            "freight": "0", "freight_payer": "自提无", "pay_status": "未付款",
            "idempotency_key": "v3-so-2"})
        check("未付单创建", s == 200, str(so2))
        s, _ = c.call("POST", f"/api/sales-orders/{so2['id']}/pay-status", {"pay_status": "已付款"})
        _, orders = c.call("GET", "/api/sales-orders")
        o2 = [o for o in orders if o["id"] == so2["id"]][0]
        check("改已付款并留痕", s == 200 and o2["pay_status"] == "已付款" and o2["pay_time"]
              and any(e["event"] == "付款状态" for e in o2["events"]), str(o2))
        _, inv = c.call("GET", "/api/inventory?view=product")
        r = [x for x in inv if x["product_name"] == "老酒甲"][0]
        check("取消前预定3", r["reserved_qty"] == 3, str(r))
        s, _ = c.call("POST", f"/api/sales-orders/{so2['id']}/cancel", {"reason": "客户不要了"})
        _, inv = c.call("GET", "/api/inventory?view=product")
        r = [x for x in inv if x["product_name"] == "老酒甲"][0]
        _, orders = c.call("GET", "/api/sales-orders")
        o2 = [o for o in orders if o["id"] == so2["id"]]
        check("取消后预定释放且单子记录保留",
              s == 200 and r["reserved_qty"] == 0 and r["qty"] == 11
              and len(o2) == 1 and o2[0]["order_status"] == "已取消" and o2[0]["cancel_reason"] == "客户不要了",
              str(r))
    finally:
        stop(proc)

    # ---------- 阶段3: 全新库注册 ----------
    print("[8] 全新库: 首个注册成管理员 + 注册开关")
    fresh = tempfile.mkdtemp(prefix="wine-fresh-")
    proc, c = start(APP_DIR, 8022, fresh, admin_pw=None)
    try:
        s, rs = c.call("GET", "/api/registration-status")
        check("全新库注册开放", rs.get("open") is True and rs.get("has_users") is False, str(rs))
        s, reg = c.call("POST", "/api/register", {"username": "boss", "password": "boss-pass-1"})
        check("首个注册成功且为管理员", s == 200 and reg.get("role") == "admin", str(reg))
        s, reg2 = c.call("POST", "/api/register", {"username": "second", "password": "second-pass-1"})
        check("有管理员后默认关注册(拒绝)", s == 403, f"got {s} {reg2}")
        s, d = c.call("POST", "/api/login", {"username": "boss", "password": "boss-pass-1"})
        check("注册账号可登录", s == 200, str(d))
        s, _ = c.call("POST", "/api/settings/registration", {"open": True})
        s, reg3 = c.call("POST", "/api/register", {"username": "second", "password": "second-pass-1"})
        check("管理员开放后可再注册", s == 200 and reg3.get("role") == "admin", str(reg3))
        s, _ = c.call("POST", "/api/settings/registration", {"open": False})
        s, reg4 = c.call("POST", "/api/register", {"username": "third", "password": "third-pass-1"})
        check("管理员关闭后拒绝注册", s == 403, f"got {s}")
    finally:
        stop(proc)

    # ---------- 阶段4: 老备份恢复到新系统 ----------
    print("[9] 老备份恢复兼容")
    if backup_path and Path(backup_path).exists():
        restore_dir = tempfile.mkdtemp(prefix="wine-restore-")
        shutil.copy(backup_path, Path(restore_dir) / "wine.db")
        proc, c = start(APP_DIR, 8023, restore_dir, admin_pw="test-pass-123")
        try:
            s, d = c.call("POST", "/api/login", {"username": "allen314", "password": "test-pass-123"})
            check("恢复后老管理员可登录", s == 200, str(d))
            _, inv = c.call("GET", "/api/inventory?view=product")
            r = [x for x in inv if x["product_name"] == "老酒甲"]
            check("恢复后老库存完好且新字段可用", len(r) == 1 and r[0]["qty"] == 12 and r[0]["available_qty"] == 12, str(r))
            s, rs = c.call("GET", "/api/registration-status")
            check("恢复后注册默认关闭(已有管理员)", rs.get("open") is False, str(rs))
        finally:
            stop(proc)
    else:
        skipped += 1
        print("  SKIP 无老备份文件")

    print(f"\n结果: {passed} 通过, {failed} 失败, {skipped} 跳过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
