#!/usr/bin/env python3
"""v0.3.0 自测: 已录入信息可更改(档案/货单/入库单/调拨/付款)+修改留痕+老库升级。
真实 HTTP。老库阶段需要 v0.2.0 快照 /tmp/wine-v020/app.py(缺失时跳过并提示)。
运行: python3 tests/test_v4.py
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
OLD_DIR = Path("/tmp/wine-v020")
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


def inv_product(c, name):
    _, rows = c.call("GET", "/api/inventory?view=product")
    return [r for r in rows if r["product_name"] == name][0]


def inv_warehouse(c, wh_name, prod_name):
    _, rows = c.call("GET", "/api/inventory?view=warehouse")
    hit = [r for r in rows if r["warehouse_name"] == wh_name and r["product_name"] == prod_name]
    return hit[0]["qty"] if hit else 0


def main():
    global skipped
    data = tempfile.mkdtemp(prefix="wine-v4-")
    print("[1] 新代码全功能修改测试")
    proc, c = start(APP_DIR, 8030, data, admin_pw="test-pass-123")
    try:
        anon = Client(8030, cookies=False)
        s, _ = anon.call("PUT", "/api/products/1", {"name": "x"})
        check("未登录修改被拒(401)", s == 401, str(s))
        s, d = c.call("POST", "/api/login", {"username": "allen314", "password": "test-pass-123"})
        check("管理员登录", s == 200, str(d))
        _, me = c.call("GET", "/api/me")
        check("版本号 v0.3.0", me.get("version") == "v0.3.0", str(me))

        _, s1 = c.call("POST", "/api/suppliers", {"name": "供应商甲"})
        _, s2 = c.call("POST", "/api/suppliers", {"name": "供应商乙"})
        _, c1 = c.call("POST", "/api/customers", {"name": "客户甲"})
        _, c2 = c.call("POST", "/api/customers", {"name": "客户乙"})
        _, w1 = c.call("POST", "/api/warehouses", {"name": "主仓"})
        _, w2 = c.call("POST", "/api/warehouses", {"name": "分仓"})
        _, pa = c.call("POST", "/api/products", {"type": "酒", "name": "酒甲", "category": "红葡萄酒", "vintage": "2018", "origin": "法国"})
        _, pb = c.call("POST", "/api/products", {"type": "酒", "name": "酒乙", "category": "白葡萄酒", "vintage": "2020", "origin": "德国"})
        _, acc = c.call("POST", "/api/products", {"type": "附件", "name": "礼品袋"})

        # 入库单 PO1: 酒甲x10@100 + 酒乙x5@200, 运费30 -> 货款2000 总计2030
        s, po1 = c.call("POST", "/api/purchase-orders", {
            "type": "采购入库", "supplier_id": s1["id"], "warehouse_id": w1["id"],
            "biz_date": "2026-11-20", "freight": "30",
            "lines": [{"product_id": pa["id"], "qty": 10, "unit_price": "100"},
                      {"product_id": pb["id"], "qty": 5, "unit_price": "200"}],
            "idempotency_key": "v4-po1"})
        check("PO1 入库", s == 200, str(po1))

        # ---- 档案修改 ----
        s, _ = c.call("PUT", f"/api/products/{pa['id']}", {
            "type": "酒", "name": "酒甲改", "category": "红葡萄酒", "vintage": "2019",
            "origin": "法国波尔多", "note": "改过"})
        check("商品改名", s == 200, str(s))
        _, prods = c.call("GET", "/api/products")
        check("商品列表显示新名", any(p["name"] == "酒甲改" and p["vintage"] == "2019" for p in prods), str(prods))
        _, pos = c.call("GET", "/api/purchase-orders")
        check("关联入库单显示新名", any(l["product_name"] == "酒甲改" for o in pos for l in o["lines"]), str(pos))
        s, _ = c.call("PUT", f"/api/suppliers/{s1['id']}", {"name": "供应商甲改", "contact": "张三", "phone": "123", "note": ""})
        _, sups = c.call("GET", "/api/suppliers")
        check("供应商修改", s == 200 and any(x["name"] == "供应商甲改" for x in sups), str(sups))
        s, _ = c.call("PUT", f"/api/customers/{c1['id']}", {"name": "客户甲", "contact": "", "phone": "999", "note": ""})
        _, custs = c.call("GET", "/api/customers")
        check("客户修改", s == 200 and any(x["phone"] == "999" for x in custs), str(custs))
        # ---- 客户/供应商字段: 客户 姓名/电话/地址/备注; 供应商 姓名/电话/备注 ----
        s, c2 = c.call("POST", "/api/customers", {"name": "客户乙", "phone": "13800000000",
                       "address": "上海市静安区南京西路1号", "note": "常客"})
        _, custs = c.call("GET", "/api/customers")
        check("客户地址录入", s == 200 and any(x["address"] == "上海市静安区南京西路1号" for x in custs), str(custs))
        s, _ = c.call("PUT", f"/api/customers/{c2['id']}", {"name": "客户乙", "phone": "13800000000",
                      "address": "北京市朝阳区2号", "note": ""})
        _, custs = c.call("GET", "/api/customers")
        check("客户地址修改", s == 200 and any(x["address"] == "北京市朝阳区2号" for x in custs), str(custs))
        s, s2 = c.call("POST", "/api/suppliers", {"name": "供应商乙", "phone": "021-555", "note": "月结"})
        _, sups = c.call("GET", "/api/suppliers")
        check("供应商三字段录入", s == 200 and any(x["name"] == "供应商乙" and x["phone"] == "021-555" for x in sups), str(sups))
        s, _ = c.call("PUT", f"/api/warehouses/{w2['id']}", {"name": "分仓", "note": "二楼"})
        _, whs = c.call("GET", "/api/warehouses")
        check("仓库修改", s == 200 and any(x["note"] == "二楼" for x in whs), str(whs))
        s, _ = c.call("PUT", "/api/products/99999", {"name": "不存在"})
        check("改不存在的商品被拒(404)", s == 404, str(s))

        # ---- 入库单修改: 酒甲x12@90 + 酒乙x5@200, 运费40 ----
        # 货款=1080+1000=2080 总计2120; 运费分摊(分): 酒甲 4000*108000//208000=2076 余1补酒甲=2077
        # 酒甲批次成本=108000+2077=110077; 酒乙批次成本=100000+1923=101923
        s, d = c.call("PUT", f"/api/purchase-orders/{po1['id']}", {
            "supplier_id": s1["id"], "warehouse_id": w1["id"], "biz_date": "2026-11-21",
            "freight": "40", "pay_status": "未付款",
            "lines": [{"product_id": pa["id"], "qty": 12, "unit_price": "90"},
                      {"product_id": pb["id"], "qty": 5, "unit_price": "200"}]})
        check("PO1 修改成功", s == 200, str(d))
        _, pos = c.call("GET", "/api/purchase-orders")
        o1 = [o for o in pos if o["id"] == po1["id"]][0]
        check("PO1 总计重算2120", o1["total_cents"] == 212000 and o1["goods_cents"] == 208000, str(o1))
        _, batches = c.call("GET", "/api/inventory?view=batch")
        ba = [b for b in batches if b["product_name"] == "酒甲改" and b["qty_initial"] == 12]
        check("酒甲批次成本手算110077", ba and ba[0]["total_cost_cents"] == 110077, str(batches))
        _, pays = c.call("GET", "/api/payables")
        pay1 = [p for p in pays if p["purchase_order_id"] == po1["id"]][0]
        check("应付重算2120", pay1["amount_cents"] == 212000, str(pay1))
        check("改后库存酒甲12", inv_product(c, "酒甲改")["qty"] == 12)

        # 部分付款1000后, 改到总计<已付被拒
        s, _ = c.call("POST", f"/api/payables/{pay1['id']}/payments",
                      {"amount": "1000", "pay_date": "2026-11-22", "note": "首付"})
        check("PO1 部分付款1000", s == 200, str(s))
        s, d = c.call("PUT", f"/api/purchase-orders/{po1['id']}", {
            "supplier_id": s1["id"], "warehouse_id": w1["id"], "biz_date": "2026-11-21",
            "freight": "0", "pay_status": "未付款",
            "lines": [{"product_id": pa["id"], "qty": 1, "unit_price": "10"}]})
        check("改到应付<已付被拒", s == 400, str(d))
        s, d = c.call("PUT", f"/api/purchase-orders/{po1['id']}", {
            "supplier_id": s1["id"], "warehouse_id": w1["id"], "biz_date": "2026-11-21",
            "freight": "40", "pay_status": "未付款",
            "lines": [{"product_id": pa["id"], "qty": 12, "unit_price": "90"},
                      {"product_id": pb["id"], "qty": 5, "unit_price": "200"}]})
        check("已有付款改回未付款被拒", s == 400, str(d))

        # ---- 货单修改(未发货) ----
        s, so1 = c.call("POST", "/api/sales-orders", {
            "customer_id": c1["id"], "warehouse_id": w1["id"], "biz_date": "2026-11-22",
            "lines": [{"product_id": pa["id"], "qty": 4, "unit_price": "150", "line_type": "销售"}],
            "freight": "0", "freight_payer": "客户付", "idempotency_key": "v4-so1"})
        check("SO1 建单", s == 200, str(so1))
        check("预定4", inv_product(c, "酒甲改")["reserved_qty"] == 4)
        s, d = c.call("PUT", f"/api/sales-orders/{so1['id']}", {
            "customer_id": c1["id"], "warehouse_id": w1["id"],
            "lines": [{"product_id": pa["id"], "qty": 6, "unit_price": "150", "line_type": "销售"}],
            "freight": "0", "freight_payer": "客户付", "logistics_no": "", "note": ""})
        check("未发货货单改数量", s == 200, str(d))
        check("预定重算6", inv_product(c, "酒甲改")["reserved_qty"] == 6)
        s, d = c.call("PUT", f"/api/sales-orders/{so1['id']}", {
            "customer_id": c1["id"], "warehouse_id": w1["id"],
            "lines": [{"product_id": pa["id"], "qty": 99, "unit_price": "150", "line_type": "销售"}],
            "freight": "0", "freight_payer": "客户付", "logistics_no": "", "note": ""})
        check("改到超可用量被拒", s == 400, str(d))
        s, d = c.call("PUT", f"/api/sales-orders/{so1['id']}", {
            "customer_id": c2["id"], "warehouse_id": w1["id"],
            "lines": [{"product_id": pa["id"], "qty": 4, "unit_price": "150", "line_type": "销售"}],
            "freight": "10", "freight_payer": "客户付", "logistics_no": "", "note": "改客户"})
        check("未发货货单改客户/运费", s == 200, str(d))
        _, sos = c.call("GET", "/api/sales-orders")
        o_so1 = [o for o in sos if o["id"] == so1["id"]][0]
        check("货单客户已改+总计610", o_so1["customer_name"] == "客户乙" and o_so1["total_cents"] == 61000, str(o_so1))
        check("预定回到4", inv_product(c, "酒甲改")["reserved_qty"] == 4)

        # ---- 发货后 ----
        s, _ = c.call("POST", f"/api/sales-orders/{so1['id']}/ship",
                      {"logistics_no": "SF1", "ship_time": ""})
        check("SO1 发货", s == 200, str(s))
        check("发货扣库存剩8", inv_product(c, "酒甲改")["qty"] == 8)
        s, d = c.call("PUT", f"/api/sales-orders/{so1['id']}", {
            "customer_id": c2["id"], "warehouse_id": w1["id"],
            "lines": [{"product_id": pa["id"], "qty": 5, "unit_price": "150", "line_type": "销售"}],
            "freight": "10", "freight_payer": "客户付", "logistics_no": "SF1", "note": "改客户"})
        check("已发货改明细被拒", s == 400, str(d))
        s, d = c.call("PUT", f"/api/sales-orders/{so1['id']}", {
            "customer_id": c2["id"], "warehouse_id": w1["id"],
            "lines": [{"product_id": pa["id"], "qty": 4, "unit_price": "150", "line_type": "销售"}],
            "freight": "10", "freight_payer": "客户付", "logistics_no": "SF2", "note": "新备注"})
        check("已发货改物流号成功", s == 200, str(d))
        _, sos = c.call("GET", "/api/sales-orders")
        o_so1 = [o for o in sos if o["id"] == so1["id"]][0]
        check("物流号已更新SF2", o_so1["logistics_no"] == "SF2" and o_so1["note"] == "新备注", str(o_so1))

        # 批次已消耗 -> 入库单不可改
        s, d = c.call("PUT", f"/api/purchase-orders/{po1['id']}", {
            "supplier_id": s1["id"], "warehouse_id": w1["id"], "biz_date": "2026-11-21",
            "freight": "40", "pay_status": "未付款",
            "lines": [{"product_id": pa["id"], "qty": 12, "unit_price": "90"},
                      {"product_id": pb["id"], "qty": 5, "unit_price": "200"}]})
        check("批次已消耗入库单改被拒", s == 400, str(d))

        # ---- 调拨修改 ----
        s, po2 = c.call("POST", "/api/purchase-orders", {
            "type": "采购入库", "supplier_id": s1["id"], "warehouse_id": w1["id"],
            "biz_date": "2026-11-22", "freight": "0",
            "lines": [{"product_id": pb["id"], "qty": 10, "unit_price": "50"}],
            "idempotency_key": "v4-po2"})
        check("PO2 入库", s == 200, str(po2))
        _, batches = c.call("GET", "/api/inventory?view=batch")
        b2 = [b for b in batches if b["product_name"] == "酒乙" and b["qty_initial"] == 10][0]
        s, t1 = c.call("POST", "/api/transfers", {
            "from_warehouse_id": w1["id"], "to_warehouse_id": w2["id"], "biz_date": "2026-11-22",
            "lines": [{"batch_id": b2["batch_id"], "qty": 4}], "idempotency_key": "v4-t1"})
        check("T1 调拨4", s == 200, str(t1))
        check("调拨后 主仓酒乙11/分仓4",
              inv_warehouse(c, "主仓", "酒乙") == 11 and inv_warehouse(c, "分仓", "酒乙") == 4)
        s, d = c.call("PUT", f"/api/transfers/{t1['id']}", {
            "to_warehouse_id": w2["id"], "biz_date": "2026-11-22",
            "lines": [{"batch_id": b2["batch_id"], "qty": 6}], "note": ""})
        check("调拨改数量6", s == 200, str(d))
        check("改后 主仓酒乙9/分仓6",
              inv_warehouse(c, "主仓", "酒乙") == 9 and inv_warehouse(c, "分仓", "酒乙") == 6)
        s, d = c.call("PUT", f"/api/transfers/{t1['id']}", {
            "to_warehouse_id": w2["id"], "biz_date": "2026-11-22",
            "lines": [{"batch_id": b2["batch_id"], "qty": 99}], "note": ""})
        check("调拨改超库存被拒", s == 400, str(d))
        # 分仓卖掉5后, 原调拨6件冲销不足
        s, so2 = c.call("POST", "/api/sales-orders", {
            "customer_id": c1["id"], "warehouse_id": w2["id"], "biz_date": "2026-11-22",
            "lines": [{"product_id": pb["id"], "qty": 5, "unit_price": "80", "line_type": "销售"}],
            "freight": "0", "freight_payer": "客户付", "idempotency_key": "v4-so2"})
        s2, _ = c.call("POST", f"/api/sales-orders/{so2['id']}/ship", {"logistics_no": "", "ship_time": ""})
        check("SO2 分仓发货5", s == 200 and s2 == 200, f"{so2} {s2}")
        s, d = c.call("PUT", f"/api/transfers/{t1['id']}", {
            "to_warehouse_id": w2["id"], "biz_date": "2026-11-22",
            "lines": [{"batch_id": b2["batch_id"], "qty": 7}], "note": ""})
        check("冲销不足调拨改被拒", s == 400, str(d))

        # ---- 付款记录修改 ----
        _, pays = c.call("GET", "/api/payables")
        pay2 = [p for p in pays if p["purchase_order_id"] == po2["id"]][0]
        check("PO2 应付500", pay2["amount_cents"] == 50000, str(pay2))
        c.call("POST", f"/api/payables/{pay2['id']}/payments",
               {"amount": "200", "pay_date": "2026-11-22", "note": "首付"})
        _, pays = c.call("GET", "/api/payables")
        pay2 = [p for p in pays if p["purchase_order_id"] == po2["id"]][0]
        pm = pay2["payments"][0]
        s, d = c.call("PUT", f"/api/payments/{pm['id']}",
                      {"amount": "300", "pay_date": "2026-11-23", "note": "改金额"})
        check("付款改金额300", s == 200, str(d))
        _, pays = c.call("GET", "/api/payables")
        pay2 = [p for p in pays if p["purchase_order_id"] == po2["id"]][0]
        check("已付总额重算300", pay2["paid_cents"] == 30000 and pay2["balance_cents"] == 20000, str(pay2))
        check("付款日期备注已改", pay2["payments"][0]["pay_date"] == "2026-11-23"
              and pay2["payments"][0]["note"] == "改金额", str(pay2))
        s, d = c.call("PUT", f"/api/payments/{pm['id']}",
                      {"amount": "600", "pay_date": "2026-11-23", "note": ""})
        check("付款改超应付被拒", s == 400, str(d))

        # ---- 已取消不可改 ----
        s, so3 = c.call("POST", "/api/sales-orders", {
            "customer_id": c1["id"], "warehouse_id": w1["id"], "biz_date": "2026-11-22",
            "lines": [{"product_id": pb["id"], "qty": 1, "unit_price": "80", "line_type": "销售"}],
            "freight": "0", "freight_payer": "客户付", "idempotency_key": "v4-so3"})
        c.call("POST", f"/api/sales-orders/{so3['id']}/cancel", {"reason": "测试"})
        s, d = c.call("PUT", f"/api/sales-orders/{so3['id']}", {
            "customer_id": c1["id"], "warehouse_id": w1["id"],
            "lines": [{"product_id": pb["id"], "qty": 2, "unit_price": "80", "line_type": "销售"}],
            "freight": "0", "freight_payer": "客户付", "logistics_no": "", "note": ""})
        check("已取消货单改被拒", s == 400, str(d))
        s, po3 = c.call("POST", "/api/purchase-orders", {
            "type": "期初库存", "supplier_id": None, "warehouse_id": w1["id"],
            "biz_date": "2026-11-22", "freight": "0",
            "lines": [{"product_id": acc["id"], "qty": 2, "unit_price": "5"}],
            "idempotency_key": "v4-po3"})
        c.call("POST", f"/api/purchase-orders/{po3['id']}/cancel", {})
        s, d = c.call("PUT", f"/api/purchase-orders/{po3['id']}", {
            "supplier_id": None, "warehouse_id": w1["id"], "biz_date": "2026-11-22",
            "freight": "0", "pay_status": "未付款",
            "lines": [{"product_id": acc["id"], "qty": 3, "unit_price": "5"}]})
        check("已作废入库单改被拒", s == 400, str(d))

        # ---- 修改留痕 ----
        s, logs = c.call("GET", "/api/edit-logs")
        types = {x["entity_type"] for x in logs}
        check("edit_logs 覆盖全部实体类型",
              {"product", "supplier", "customer", "warehouse",
               "purchase_order", "sales_order", "transfer", "payment"} <= types, str(types))
        ok_json = all(json.loads(x["before_json"]) is not None and json.loads(x["after_json"]) is not None
                      for x in logs)
        check("edit_logs 改前改后均为合法JSON", ok_json and len(logs) >= 10, f"count={len(logs)}")
        s, logs1 = c.call("GET", f"/api/edit-logs?entity_type=product&entity_id={pa['id']}")
        check("edit_logs 按实体过滤", s == 200 and len(logs1) >= 1
              and "酒甲改" in logs1[0]["after_json"], str(logs1))
    finally:
        stop(proc)

    # ---------- 老库升级 ----------
    print("[2] v0.2.0 老库升级兼容")
    old_data = tempfile.mkdtemp(prefix="wine-v4-old-")
    if (OLD_DIR / "app.py").exists():
        proc, c = start(OLD_DIR, 8031, old_data, admin_pw="test-pass-123")
        try:
            c.call("POST", "/api/login", {"username": "allen314", "password": "test-pass-123"})
            _, sup = c.call("POST", "/api/suppliers", {"name": "老供应商"})
            _, wh = c.call("POST", "/api/warehouses", {"name": "老仓"})
            _, pr = c.call("POST", "/api/products", {"type": "酒", "name": "老酒", "vintage": "2015"})
            s, o = c.call("POST", "/api/purchase-orders", {
                "type": "采购入库", "supplier_id": sup["id"], "warehouse_id": wh["id"],
                "biz_date": "2026-11-20", "freight": "0",
                "lines": [{"product_id": pr["id"], "qty": 3, "unit_price": "80"}],
                "idempotency_key": "v4-old-1"})
            check("老版建库录数据", s == 200, str(o))
            old_pid = pr["id"]
        finally:
            stop(proc)
        proc, c = start(APP_DIR, 8032, old_data, admin_pw="test-pass-123")
        try:
            s, d = c.call("POST", "/api/login", {"username": "allen314", "password": "test-pass-123"})
            check("老管理员升级后可登录", s == 200, str(d))
            _, prods = c.call("GET", "/api/products")
            check("老商品完好", any(p["name"] == "老酒" for p in prods), str(prods))
            check("老库存完好(3件)", inv_product(c, "老酒")["qty"] == 3)
            s, d = c.call("PUT", f"/api/products/{old_pid}", {
                "type": "酒", "name": "老酒改", "category": "", "vintage": "2015",
                "origin": "", "note": ""})
            check("老库升级后修改可用", s == 200, str(d))
            s, logs = c.call("GET", "/api/edit-logs")
            check("老库升级后 edit_logs 可用", s == 200 and len(logs) >= 1, str(logs))
        finally:
            stop(proc)
    else:
        skipped += 1
        print("[2] SKIP 老库阶段: /tmp/wine-v020/app.py 不存在")

    print(f"\n结果: {passed} 通过 / {failed} 失败 / {skipped} 跳过")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
