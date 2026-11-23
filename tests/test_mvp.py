#!/usr/bin/env python3
"""一期入库 MVP 自测: 起临时服务+临时库, 用 urllib 走真实 HTTP。
运行: python3 tests/test_mvp.py
"""
import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PORT = 8019
BASE = f"http://127.0.0.1:{PORT}"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
passed = failed = 0


def call(method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, (r.read() if raw else json.loads(r.read() or b"{}"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def main():
    tmp = tempfile.mkdtemp(prefix="wine-test-")
    env = dict(os.environ, WINE_DATA_DIR=tmp, WINE_ADMIN_PASSWORD="test-pass-123")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=APP_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                s, _d = call("GET", "/api/me")
                break
            except Exception:
                time.sleep(0.3)
        else:
            print(proc.stdout.read() if proc.stdout else "")
            raise SystemExit("服务没起来")

        print("[1] 未登录拦截 + 登录")
        s, _ = call("GET", "/api/products")
        check("未登录访问档案被拒(401)", s == 401, f"got {s}")
        s, d = call("POST", "/api/login", {"username": "allen314", "password": "test-pass-123"})
        check("管理员登录", s == 200 and d.get("ok"), str(d))

        print("[2] 档案")
        _, sup = call("POST", "/api/suppliers", {"name": "测试供应商"})
        _, wha = call("POST", "/api/warehouses", {"name": "主仓"})
        _, whb = call("POST", "/api/warehouses", {"name": "分仓"})
        _, w1 = call("POST", "/api/products", {"type": "酒", "name": "测试红酒甲", "category": "红葡萄酒", "vintage": "2018", "origin": "法国波尔多"})
        _, w2 = call("POST", "/api/products", {"type": "酒", "name": "测试红酒乙", "category": "红葡萄酒", "vintage": "2020", "origin": "智利"})
        _, acc = call("POST", "/api/products", {"type": "附件", "name": "礼品袋"})
        check("档案创建", all(x.get("id") for x in (sup, wha, whb, w1, w2, acc)))

        print("[3] 无运费入库: 甲 12瓶 × ¥100 = ¥1200")
        s, o1 = call("POST", "/api/purchase-orders", {
            "type": "采购入库", "supplier_id": sup["id"], "warehouse_id": wha["id"],
            "biz_date": "2026-11-23", "freight": "0",
            "lines": [{"product_id": w1["id"], "qty": 12, "unit_price": "100"}],
            "idempotency_key": "test-key-1"})
        check("入库单创建", s == 200 and o1.get("order_no"), str(o1))
        _, batches = call("GET", "/api/inventory?view=batch")
        b1 = [b for b in batches if b["product_name"] == "测试红酒甲"][0]
        check("批次剩余12", b1["qty_remaining"] == 12, str(b1))
        check("批次实际成本 ¥100/瓶", abs(b1["unit_cost"] - 100) < 0.001, str(b1["unit_cost"]))
        _, pays = call("GET", "/api/payables")
        check("应付 ¥1200", len(pays) == 1 and pays[0]["amount_cents"] == 120000, str(pays))

        print("[4] 幂等: 同 key 重发不重复入账")
        s, dup = call("POST", "/api/purchase-orders", {
            "type": "采购入库", "supplier_id": sup["id"], "warehouse_id": wha["id"],
            "biz_date": "2026-11-23", "freight": "0",
            "lines": [{"product_id": w1["id"], "qty": 12, "unit_price": "100"}],
            "idempotency_key": "test-key-1"})
        _, pays2 = call("GET", "/api/payables")
        check("重复提交被忽略", dup.get("duplicated") is True and len(pays2) == 1, str(dup))

        print("[5] 有运费入库(手算): 甲10×¥200=¥2000, 乙10×¥100=¥1000, 运费¥300")
        print("    按金额比 2:1 分摊 → 甲摊¥200(成本¥220/瓶), 乙摊¥100(成本¥110/瓶), 应付¥3300")
        s, o2 = call("POST", "/api/purchase-orders", {
            "type": "采购入库", "supplier_id": sup["id"], "warehouse_id": wha["id"],
            "biz_date": "2026-11-23", "freight": "300",
            "lines": [{"product_id": w1["id"], "qty": 10, "unit_price": "200"},
                      {"product_id": w2["id"], "qty": 10, "unit_price": "100"}],
            "idempotency_key": "test-key-2"})
        check("入库单创建", s == 200, str(o2))
        _, batches = call("GET", "/api/inventory?view=batch")
        bj = [b for b in batches if b["product_name"] == "测试红酒甲" and b["qty_initial"] == 10][0]
        by = [b for b in batches if b["product_name"] == "测试红酒乙"][0]
        check("甲批次成本 ¥220/瓶", abs(bj["unit_cost"] - 220) < 0.001, str(bj["unit_cost"]))
        check("乙批次成本 ¥110/瓶", abs(by["unit_cost"] - 110) < 0.001, str(by["unit_cost"]))
        _, pays = call("GET", "/api/payables")
        p2 = [p for p in pays if p["order_no"] == o2["order_no"]][0]
        check("应付 ¥3300", p2["amount_cents"] == 330000, str(p2))

        print("[6] 期初库存: 礼品袋 20 × ¥5, 不生成应付")
        before = len(pays)
        s, o3 = call("POST", "/api/purchase-orders", {
            "type": "期初库存", "supplier_id": None, "warehouse_id": wha["id"],
            "biz_date": "2026-11-23", "freight": "0",
            "lines": [{"product_id": acc["id"], "qty": 20, "unit_price": "5"}],
            "idempotency_key": "test-key-3"})
        _, pays = call("GET", "/api/payables")
        check("期初不生成应付", s == 200 and len(pays) == before, str(o3))

        print("[7] 多仓 + 调拨(无资金变化)")
        _, wh_view = call("GET", "/api/inventory?view=warehouse")
        main_qty = sum(r["qty"] for r in wh_view if r["warehouse_name"] == "主仓")
        check("调拨前主仓共52件(12+10+10+20)", main_qty == 52, str(main_qty))
        due_before = sum(p["balance_cents"] for p in pays)
        s, tr = call("POST", "/api/transfers", {
            "from_warehouse_id": wha["id"], "to_warehouse_id": whb["id"],
            "biz_date": "2026-11-23", "lines": [{"batch_id": b1["batch_id"], "qty": 5}],
            "idempotency_key": "test-tr-1"})
        check("调拨成功", s == 200 and tr.get("transfer_no"), str(tr))
        _, wh_view = call("GET", "/api/inventory?view=warehouse")
        qa = sum(r["qty"] for r in wh_view if r["warehouse_name"] == "主仓")
        qb = sum(r["qty"] for r in wh_view if r["warehouse_name"] == "分仓")
        check("主仓47 / 分仓5", qa == 47 and qb == 5, f"{qa}/{qb}")
        _, pays = call("GET", "/api/payables")
        check("调拨无资金变化", sum(p["balance_cents"] for p in pays) == due_before)
        s, bad = call("POST", "/api/transfers", {
            "from_warehouse_id": whb["id"], "to_warehouse_id": wha["id"],
            "biz_date": "2026-11-23", "lines": [{"batch_id": b1["batch_id"], "qty": 99}],
            "idempotency_key": "test-tr-2"})
        check("超量调拨被拒", s == 400, f"got {s}")

        print("[8] 部分付款: 第一笔应付 ¥1200 付 ¥500 + ¥700")
        p1 = [p for p in pays if p["amount_cents"] == 120000][0]
        s, r = call("POST", f"/api/payables/{p1['id']}/payments", {"amount": "500", "pay_date": "2026-11-23"})
        check("付500后余额¥700", s == 200 and r.get("balance") == 700.0, str(r))
        s, r = call("POST", f"/api/payables/{p1['id']}/payments", {"amount": "700", "pay_date": "2026-11-23"})
        check("再付700后余额¥0", s == 200 and r.get("balance") == 0.0, str(r))
        s, _r = call("POST", f"/api/payables/{p1['id']}/payments", {"amount": "1", "pay_date": "2026-11-23"})
        check("超付被拒", s == 400, f"got {s}")

        print("[9] 作废重录: 新单作废后库存冲销")
        s, o4 = call("POST", "/api/purchase-orders", {
            "type": "采购入库", "supplier_id": sup["id"], "warehouse_id": wha["id"],
            "biz_date": "2026-11-23", "freight": "0",
            "lines": [{"product_id": w2["id"], "qty": 3, "unit_price": "50"}],
            "idempotency_key": "test-key-4"})
        s, _ = call("POST", f"/api/purchase-orders/{o4['id']}/cancel", {})
        _, prod_view = call("GET", "/api/inventory?view=product")
        yqty = [r for r in prod_view if r["product_name"] == "测试红酒乙"][0]["qty"]
        check("作废后乙库存回到10", s == 200 and yqty == 10, f"{s}/{yqty}")

        print("[10] 备份 + 首页")
        s, bk = call("POST", "/api/backup", {})
        check("备份生成文件", s == 200 and bk.get("file") and Path(bk["path"]).exists(), str(bk))
        s, html = call("GET", "/", raw=True)
        check("首页可打开", s == 200 and "红酒进销存".encode() in html)

        print(f"\n结果: {passed} 通过, {failed} 失败")
        return 0 if failed == 0 else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
