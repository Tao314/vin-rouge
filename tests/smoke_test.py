"""vin-rouge v2 smoke test: 对真实运行的 uvicorn 服务跑完整流程并断言.
用法: python3 tests/smoke_test.py  (需先 pip install -r requirements.txt)
用临时数据库 VIN_ROUGE_DB, 在 127.0.0.1:9000 起服务, 测完关掉.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("PORT", "9000"))
ROOT = f"http://127.0.0.1:{PORT}"
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def req(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(ROOT + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "image" in ctype:
                return resp.status, raw
            if "application/json" in ctype:
                return resp.status, (json.loads(raw) if raw else {})
            return resp.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}


def main():
    tmp = tempfile.mkdtemp(prefix="vr-smoke-")
    db = os.path.join(tmp, "test.db")
    env = dict(os.environ, VIN_ROUGE_DB=db,
               PATH=os.environ["PATH"])
    venv_uvicorn = str(BASE / ".venv" / "bin" / "uvicorn")
    proc = subprocess.Popen(
        [venv_uvicorn, "app:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(BASE), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(30):
            try:
                s, _ = req("GET", "/")
                if s == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            check("服务启动 + 首页 200", False, "起不来")
            return 1
        check("服务启动 + 首页 200", True)

        # 1. 未登录调业务接口 -> 401
        s, _ = req("GET", "/api/wines")
        check("未登录调业务接口返回 401", s == 401, f"status={s}")

        # 1b. 版本接口公开返回 v2.4.0（期望值写死在测试里，运行时版本以 app.py 的 APP_VERSION 为准）
        s, d = req("GET", "/api/version")
        check("版本接口 /api/version 返回 v2.4.0", s == 200 and d.get("version") == "v2.4.0", f"status={s} {d}")
        s, d = req("GET", "/api/config")
        check("配置接口 /api/config 版本一致", s == 200 and d.get("version") == "v2.4.0" and "build_date" in d, f"status={s} {d}")

        # 2. 无默认用户: 首位注册前注册开放, 默认账号登录失败
        s, d = req("GET", "/api/auth/status")
        check("注册状态: 无用户时注册开放", s == 200 and d.get("registration_open") is True, f"status={s} {d}")
        s, _ = req("POST", "/api/login", {"username": "tristan", "password": "vin123456"})
        check("无默认用户: tristan/vin123456 登录失败", s == 401, f"status={s}")
        s, _ = req("POST", "/api/register", {"username": "tristan", "password": "123"})
        check("注册密码少于 6 位被拦", s == 400, f"status={s}")
        s, d = req("POST", "/api/register", {"username": "tristan", "password": "FirstPass123"})
        check("首位管理员注册成功", s == 200 and "token" in d, f"status={s} {d}")
        check("首位管理员注册后无需强制改密",
              d.get("must_change_password") is False, str(d.get("must_change_password")))
        tok = d.get("token", "")
        s, _ = req("POST", "/api/register", {"username": "someone", "password": "SomePass123"})
        check("首位注册后再次注册被拦 403", s == 403, f"status={s}")
        s, d = req("GET", "/api/auth/status")
        check("注册状态: 有用户后注册关闭", s == 200 and d.get("registration_open") is False, f"status={s} {d}")
        s, d = req("GET", "/api/wines", token=tok)
        check("首位管理员注册后能进业务页", s == 200 and isinstance(d, list), f"status={s}")

        # 3. 首位管理员改密后仍能登录
        s, d = req("POST", "/api/change-password",
                   {"old_password": "FirstPass123", "new_password": "NewPass123"},
                   token=tok)
        check("首位管理员改密成功", s == 200, f"status={s} {d}")
        s, d = req("POST", "/api/login", {"username": "tristan", "password": "NewPass123"})
        check("改密后登录成功", s == 200 and "token" in d, f"status={s}")
        tok = d.get("token", "")

        # 4. 新建第二个管理员, 用它登录, 能进货、能看到进价和利润 (不分角色)
        s, d = req("POST", "/api/users",
                   {"username": "admin2",
                    "password": "Admin2123", "must_change_password": False}, token=tok)
        check("新建第二个管理员", s == 200, f"status={s} {d}")
        s, d2 = req("POST", "/api/login", {"username": "admin2", "password": "Admin2123"})
        tok2 = d2.get("token", "")
        check("第二个管理员登录成功", s == 200 and bool(tok2), f"status={s}")
        s, d = req("GET", "/api/dashboard", token=tok2)
        check("第二个管理员能看首页(含毛利)", s == 200 and "today_profit" in d,
              f"status={s}")

        # 5. 停用该账号后它再登录失败
        s, _ = req("PUT", "/api/users/2", {"active": 0}, token=tok)
        check("停用第二个管理员", s == 200, f"status={s}")
        s, _ = req("POST", "/api/login", {"username": "admin2", "password": "Admin2123"})
        check("停用后该账号登录失败", s == 401, f"status={s}")

        # 6. 建酒
        s, w = req("POST", "/api/wines",
                   {"barcode": "6901234567890", "name": "测试红酒",
                    "brand": "测试牌", "spec": "750ml*6瓶/箱"}, token=tok)
        wid = w.get("id")
        check("建酒", s == 200 and wid, f"status={s} {w}")
        s, sups = req("GET", "/api/suppliers", token=tok)
        sup = sups[0]["id"]
        s, cuss = req("GET", "/api/customers", token=tok)
        cus = cuss[0]["id"]
        s, whs = req("GET", "/api/warehouses", token=tok)
        wh1, wh2 = whs[0]["id"], whs[1]["id"]

        # 7. 补登 10 瓶 (成本 100): 立即生效
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 10, "unit_price": 100}]},
                   token=tok)
        check("补登 10 瓶", s == 200, f"status={s} {d}")
        opening_id = d.get("id")
        s, d = req("GET", f"/api/inbound/{opening_id}", token=tok)
        check("补登单状态为已入库",
              s == 200 and d.get("status") == "received", f"status={s} {d}")
        s, d = req("POST", f"/api/inbound/{opening_id}/confirm", {}, token=tok)
        check("补登单确认到货被拒 400", s == 400, f"status={s} {d}")

        # 8. 进货 5 瓶 (进价 120, 运费 30, 货款应为 600 不含运费):
        # 下单时不加库存、不算欠款, 状态待到货
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "carrier": "顺丰", "tracking_no": "SF123", "freight": 30,
                    "paid_amount": 0,
                    "items": [{"wine_id": wid, "qty": 5, "unit_price": 120}]},
                   token=tok)
        in_id = d.get("id")
        check("进货 5 瓶", s == 200, f"status={s} {d}")
        check("运费与货款分开 (goods_total=600 不含运费30)",
              d.get("goods_total") == 600 and d.get("freight") == 30, str(d))
        check("下单后状态为待到货", d.get("status") == "pending", str(d))
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("下单后库存仍为 10 (未确认不加库存)", stock == 10, f"stock={stock}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("下单后供应商欠款仍为 0 (未确认不算欠款)",
              db.get("supplier_debt") == 0, str(db))
        # 8b. 分批确认: 先确认 2 瓶
        s, d = req("GET", f"/api/inbound/{in_id}", token=tok)
        item_id = d["items"][0]["id"]
        s, d = req("POST", f"/api/inbound/{in_id}/confirm",
                   {"items": [{"item_id": item_id, "qty": 2}]}, token=tok)
        check("部分确认到货 2 瓶",
              s == 200 and d.get("status") == "partial", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("部分确认后库存 12", stock == 12, f"stock={stock}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("部分确认后供应商欠款 240",
              db.get("supplier_debt") == 240, str(db))
        # 8c. 确认剩余 3 瓶 (不传 items 默认确认剩余全部)
        s, d = req("POST", f"/api/inbound/{in_id}/confirm", {}, token=tok)
        check("确认剩余 3 瓶后状态为已入库",
              s == 200 and d.get("status") == "received", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("全部确认后库存 15", stock == 15, f"stock={stock}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("全部确认后供应商欠款 600",
              db.get("supplier_debt") == 600, str(db))
        # 8d. 已入库的单再确认被拒
        s, d = req("POST", f"/api/inbound/{in_id}/confirm", {}, token=tok)
        check("已入库的单再确认被拒 400", s == 400, f"status={s} {d}")

        # 9. 出货 3 瓶 (卖价 200): 下单不减库存, 确认后才减
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1, "freight": 10,
                    "items": [{"wine_id": wid, "qty": 3, "unit_price": 200}]},
                   token=tok)
        out_id = d.get("id")
        check("出货 3 瓶", s == 200, f"status={s} {d}")
        check("下单后状态为待出货", d.get("status") == "pending", str(d))
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("下单后库存仍为 15 (未确认不减库存)", stock == 15, f"stock={stock}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("下单后今日销售额仍为 0 (未确认不算销售额)",
              db.get("today_sales") == 0, str(db))
        s, d = req("POST", f"/api/outbound/{out_id}/confirm", {}, token=tok)
        check("确认出货 3 瓶后状态为已出货",
              s == 200 and d.get("status") == "shipped", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("确认出货后库存剩 12", stock == 12, f"stock={stock}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("确认出货后今日销售额 600",
              db.get("today_sales") == 600, str(db))

        # 10. 出货 99 瓶被拦住 (建单时校验库存)
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 99, "unit_price": 200}]},
                   token=tok)
        check("出货 99 瓶被拦住", s == 400, f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("被拦后库存仍为 12", stock == 12, f"stock={stock}")

        # 11. 调拨 2 瓶 主仓->副仓: 下单不调库存, 确认后才调
        s, d = req("POST", "/api/transfers",
                   {"wine_id": wid, "from_warehouse_id": wh1,
                    "to_warehouse_id": wh2, "qty": 2}, token=tok)
        check("调拨 2 瓶", s == 200, f"status={s} {d}")
        check("下单后状态为待调拨", d.get("status") == "pending", str(d))
        s, st = req("GET", "/api/stock", token=tok)
        m = {(x["warehouse_id"], x["wine_id"]): x["quantity"] for x in st}
        check("下单后主仓仍为 12 / 副仓 0",
              m.get((wh1, wid)) == 12 and m.get((wh2, wid), 0) == 0, str(m))
        s, ds = req("GET", "/api/transfers", token=tok)
        tr_id = [x for x in ds if x["transfer_no"] == d.get("transfer_no")][0]["id"]
        s, d = req("POST", f"/api/transfers/{tr_id}/confirm", {}, token=tok)
        check("确认调拨后状态为已完成",
              s == 200 and d.get("status") == "done", f"status={s} {d}")
        s, st = req("GET", "/api/stock", token=tok)
        m = {(x["warehouse_id"], x["wine_id"]): x["quantity"] for x in st}
        check("确认调拨后主仓 10 / 副仓 2",
              m.get((wh1, wid)) == 10 and m.get((wh2, wid)) == 2, str(m))
        s, d = req("POST", f"/api/transfers/{tr_id}/confirm", {}, token=tok)
        check("已完成的调拨再确认被拒 400", s == 400, f"status={s} {d}")

        # 12. 补记收款后出货单变已付
        s, d = req("POST", f"/api/orders/outbound/{out_id}/payments",
                   {"amount": 600}, token=tok)
        check("补记收款后状态变已付",
              s == 200 and d.get("pay_status") == "paid", f"status={s} {d}")

        # 13. 两种分享图都是有效 PNG
        from PIL import Image
        import io
        for otype, oid, name in (("outbound", out_id, "出货分享图"),
                                 ("inbound", in_id, "进货分享图")):
            s, raw = req("GET", f"/api/orders/{otype}/{oid}/share.png", token=tok)
            ok = False
            try:
                im = Image.open(io.BytesIO(raw))
                im.verify()
                ok = s == 200 and im.format == "PNG" and im.width == 1080
            except Exception as e:
                raw = b""
            check(f"{name}为有效 PNG (宽1080)", ok, f"status={s}")

        # 15. 自饮 2 瓶: 库存减 2, 不产生应收/不计入销售额
        s, d = req("POST", "/api/outbound",
                   {"type": "personal", "warehouse_id": wh1, "note": "周末朋友聚会",
                    "items": [{"wine_id": wid, "qty": 2}]}, token=tok)
        check("自饮 2 瓶", s == 200 and d.get("sale_total") == 0, f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("自饮后库存剩 10", stock == 10, f"stock={stock}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("自饮不计入今日销售额/毛利",
              db.get("today_sales") == 600 and db.get("today_profit") == 240, str(db))
        check("自饮本月统计 2 瓶/成本 240",
              db.get("month_personal_qty") == 2 and db.get("month_personal_cost") == 240,
              str(db))

        # 16. 赠送 1 瓶给客户: 库存减 1, 按客户可查累计, 无应收、补记收款被拒
        s, d = req("POST", "/api/outbound",
                   {"type": "gift", "customer_id": cus, "warehouse_id": wh1,
                    "note": "中秋送礼",
                    "items": [{"wine_id": wid, "qty": 1}]}, token=tok)
        gift_id = d.get("id")
        check("赠送 1 瓶给客户",
              s == 200 and d.get("sale_total") == 0 and d.get("pay_status") == "paid",
              f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("赠送后库存剩 9", stock == 9, f"stock={stock}")
        s, g = req("GET", f"/api/customers/{cus}/gifts", token=tok)
        check("按客户查到赠送累计 1 瓶/成本 120",
              s == 200 and g.get("total_qty") == 1 and g.get("total_cost") == 120,
              f"status={s} {g}")
        s, d = req("POST", f"/api/orders/outbound/{gift_id}/payments",
                   {"amount": 10}, token=tok)
        check("赠送单补记收款被拒", s == 400, f"status={s} {d}")

        # 17. 透支自饮被拦
        s, d = req("POST", "/api/outbound",
                   {"type": "personal", "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 99}]}, token=tok)
        check("透支自饮 99 瓶被拦住", s == 400, f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("透支被拦后库存仍为 9", stock == 9, f"stock={stock}")

        # 18. 破损 1 瓶: 库存减 1, 不产生应收
        s, d = req("POST", "/api/outbound",
                   {"type": "damage", "warehouse_id": wh1, "note": "搬运摔了",
                    "items": [{"wine_id": wid, "qty": 1}]}, token=tok)
        check("破损 1 瓶", s == 200 and d.get("sale_total") == 0, f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("破损后库存剩 8", stock == 8, f"stock={stock}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("破损本月统计 1 瓶/成本 120",
              db.get("month_damage_qty") == 1 and db.get("month_damage_cost") == 120,
              str(db))

        # 19. 折扣出货: 原价 200 的酒 9 折出 2 瓶, 实收 360, 折扣 40, 利润按折后算
        s, d = req("POST", "/api/outbound",
                   {"type": "sale", "customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 2, "orig_price": 200,
                               "discount_rate": 0.9, "reason": "瓶身划痕"}]}, token=tok)
        disc_id = d.get("id")
        check("9 折出 2 瓶实收 360/折扣 40",
              s == 200 and d.get("sale_total") == 360 and d.get("discount_total") == 40,
              f"status={s} {d}")
        s, d = req("POST", f"/api/outbound/{disc_id}/confirm", {}, token=tok)
        check("确认折扣出货后状态为已出货",
              s == 200 and d.get("status") == "shipped", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("折扣出货后库存剩 6", stock == 6, f"stock={stock}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("折扣后今日销售额 960/毛利按折后算 360",
              db.get("today_sales") == 960 and db.get("today_profit") == 360, str(db))

        # 20. 分享图: 折扣销售单有图, 赠送单被拒; 打折无原因被拒
        s, raw = req("GET", f"/api/orders/outbound/{disc_id}/share.png", token=tok)
        ok = False
        try:
            im = Image.open(io.BytesIO(raw))
            im.verify()
            ok = s == 200 and im.format == "PNG" and im.width == 1080
        except Exception:
            pass
        check("折扣出货分享图为有效 PNG (宽1080)", ok, f"status={s}")
        s, d = req("GET", f"/api/orders/outbound/{gift_id}/share.png", token=tok)
        check("赠送单分享图被拒", s == 400, f"status={s} {d}")
        s, d = req("POST", "/api/outbound",
                   {"type": "sale", "customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 1, "orig_price": 200,
                               "discount_rate": 0.9}]}, token=tok)
        check("打折无原因被拒", s == 400, f"status={s} {d}")

        # 13b. 停用后下拉不再出现（前端下拉只取 active 的，后端停用后立即刷新）
        s, d = req("POST", "/api/customers",
                   {"name": "临时客户", "phone": "13600000000"}, token=tok)
        tmp_cus = d.get("id")
        check("新建临时客户", s == 200 and tmp_cus, f"status={s} {d}")
        s, _ = req("PUT", f"/api/customers/{tmp_cus}", {"active": 0}, token=tok)
        check("停用临时客户", s == 200, f"status={s}")
        s, cuss = req("GET", "/api/customers", None, token=tok)
        row = [c for c in cuss if c["id"] == tmp_cus][0]
        check("停用后客户 active 为 0", row["active"] == 0, f"status={s} {row}")
        visible = [c for c in cuss if c["active"]]
        check("停用后下拉（仅 active）不再出现该客户",
              all(c["id"] != tmp_cus for c in visible), f"status={s}")
        s, _ = req("PUT", f"/api/customers/{tmp_cus}", {"active": 1}, token=tok)
        check("重新启用客户", s == 200, f"status={s}")

        # 13c. 客户改名
        s, _ = req("PUT", f"/api/customers/{tmp_cus}", {"name": "临时客户改名"}, token=tok)
        check("客户改名", s == 200, f"status={s}")
        s, cuss = req("GET", "/api/customers", None, token=tok)
        row = [c for c in cuss if c["id"] == tmp_cus][0]
        check("改名后客户名已更新", row["name"] == "临时客户改名", f"status={s} {row}")

        # 13d. 客户无单可删、有单拒删
        s, _ = req("DELETE", f"/api/customers/{tmp_cus}", None, token=tok)
        check("无单客户可删除", s == 200, f"status={s}")
        s, cuss = req("GET", "/api/customers", None, token=tok)
        check("删除后客户不在列表", all(c["id"] != tmp_cus for c in cuss), f"status={s}")
        s, d = req("DELETE", f"/api/customers/{cus}", None, token=tok)
        check("有出货单的客户拒删 400", s == 400, f"status={s} {d}")
        s, _ = req("DELETE", "/api/customers/99999", None, token=tok)
        check("删除不存在的客户 404", s == 404, f"status={s}")

        # 13e. 供应商改名、可删、拒删
        s, d = req("POST", "/api/suppliers",
                   {"name": "临时供应商", "phone": "13700000000"}, token=tok)
        tmp_sup = d.get("id")
        check("新建临时供应商", s == 200 and tmp_sup, f"status={s} {d}")
        s, _ = req("PUT", f"/api/suppliers/{tmp_sup}", {"name": "临时供应商改名"}, token=tok)
        check("供应商改名", s == 200, f"status={s}")
        s, sups = req("GET", "/api/suppliers", None, token=tok)
        row = [x for x in sups if x["id"] == tmp_sup][0]
        check("改名后供应商名已更新", row["name"] == "临时供应商改名", f"status={s} {row}")
        s, _ = req("DELETE", f"/api/suppliers/{tmp_sup}", None, token=tok)
        check("无单供应商可删除", s == 200, f"status={s}")
        s, d = req("DELETE", f"/api/suppliers/{sup}", None, token=tok)
        check("有进货单的供应商拒删 400", s == 400, f"status={s} {d}")
        s, _ = req("DELETE", "/api/suppliers/99999", None, token=tok)
        check("删除不存在的供应商 404", s == 404, f"status={s}")

        # 13f. 供应商停用后下拉不再出现
        s, d = req("POST", "/api/suppliers",
                   {"name": "临时供应商2", "phone": "13700000001"}, token=tok)
        tmp_sup2 = d.get("id")
        s, _ = req("PUT", f"/api/suppliers/{tmp_sup2}", {"active": 0}, token=tok)
        s, sups = req("GET", "/api/suppliers", None, token=tok)
        visible = [x for x in sups if x["active"]]
        check("停用后供应商下拉（仅 active）不再出现",
              all(x["id"] != tmp_sup2 for x in visible), f"status={s}")
        s, _ = req("DELETE", f"/api/suppliers/{tmp_sup2}", None, token=tok)
        check("停用后无单供应商仍可删除", s == 200, f"status={s}")


        # 21. v2.3.0: 多酒品、幂等、作废、默认值、显示名去掉
        # 21a. 再建一款酒, 补登 5 瓶
        s, w2 = req("POST", "/api/wines",
                    {"barcode": "6901234567891", "name": "测试白酒",
                     "brand": "测试牌", "spec": "500ml*6瓶/箱"}, token=tok)
        wid2 = w2.get("id")
        check("建第二款酒", s == 200 and wid2, f"status={s} {w2}")
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid2, "qty": 5, "unit_price": 50}]},
                   token=tok)
        check("第二款酒补登 5 瓶", s == 200, f"status={s} {d}")

        # 21b. 多酒品进货: 两行, 货款 2*120+3*50=390 (下单不加库存, 确认后加)
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 2, "unit_price": 120},
                              {"wine_id": wid2, "qty": 3, "unit_price": 50}]},
                   token=tok)
        multi_in = d.get("id")
        check("多酒品进货 2 行货款 390",
              s == 200 and d.get("goods_total") == 390, f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        st2 = [w["stock_total"] for w in ws if w["id"] == wid2][0]
        check("下单后库存仍为 6 / 5", st1 == 6 and st2 == 5, f"{st1}/{st2}")
        s, d = req("POST", f"/api/inbound/{multi_in}/confirm", {}, token=tok)
        check("确认多酒品进货后状态为已入库",
              s == 200 and d.get("status") == "received", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        st2 = [w["stock_total"] for w in ws if w["id"] == wid2][0]
        check("多酒品进货确认后库存 8 / 8", st1 == 8 and st2 == 8, f"{st1}/{st2}")

        # 21c. 多酒品出货: 两行, 1*200+1*80=280 (下单不减库存, 确认后减)
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 1, "unit_price": 200},
                              {"wine_id": wid2, "qty": 1, "unit_price": 80}]},
                   token=tok)
        multi_out = d.get("id")
        check("多酒品出货 2 行总额 280",
              s == 200 and d.get("sale_total") == 280, f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        st2 = [w["stock_total"] for w in ws if w["id"] == wid2][0]
        check("下单后库存仍为 8 / 8", st1 == 8 and st2 == 8, f"{st1}/{st2}")
        s, d = req("POST", f"/api/outbound/{multi_out}/confirm", {}, token=tok)
        check("确认多酒品出货后状态为已出货",
              s == 200 and d.get("status") == "shipped", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        st2 = [w["stock_total"] for w in ws if w["id"] == wid2][0]
        check("多酒品出货确认后库存 7 / 7", st1 == 7 and st2 == 7, f"{st1}/{st2}")

        # 21d. 幂等: 同一个 key 重复建单只建一次 (建单不加库存, 确认一次才动)
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "idempotency_key": "idem-in-1",
                    "items": [{"wine_id": wid, "qty": 1, "unit_price": 120}]},
                   token=tok)
        idem_no = d.get("order_no")
        idem_in_id = d.get("id")
        check("幂等进货第一次成功", s == 200 and idem_no, f"status={s} {d}")
        s, d2 = req("POST", "/api/inbound",
                    {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                     "idempotency_key": "idem-in-1",
                     "items": [{"wine_id": wid, "qty": 1, "unit_price": 120}]},
                    token=tok)
        check("幂等进货第二次返回原单",
              s == 200 and d2.get("order_no") == idem_no
              and d2.get("duplicate") is True, f"status={s} {d2}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("幂等进货重复提交库存仍为 7 (未确认不加)", st1 == 7, f"stock={st1}")
        s, d = req("POST", f"/api/inbound/{idem_in_id}/confirm", {}, token=tok)
        check("幂等进货确认一次后库存 8",
              s == 200 and d.get("status") == "received", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("幂等进货确认后库存 8", st1 == 8, f"stock={st1}")
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "idempotency_key": "idem-out-1",
                    "items": [{"wine_id": wid, "qty": 1, "unit_price": 200}]},
                   token=tok)
        idem_out_no = d.get("order_no")
        idem_out_id = d.get("id")
        check("幂等出货第一次成功", s == 200 and idem_out_no, f"status={s} {d}")
        s, d2 = req("POST", "/api/outbound",
                    {"customer_id": cus, "warehouse_id": wh1,
                     "idempotency_key": "idem-out-1",
                     "items": [{"wine_id": wid, "qty": 1, "unit_price": 200}]},
                    token=tok)
        check("幂等出货第二次返回原单",
              s == 200 and d2.get("order_no") == idem_out_no
              and d2.get("duplicate") is True, f"status={s} {d2}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("幂等出货重复提交库存仍为 8 (未确认不减)", st1 == 8, f"stock={st1}")
        s, d = req("POST", f"/api/outbound/{idem_out_id}/confirm", {}, token=tok)
        check("幂等出货确认一次后库存 7",
              s == 200 and d.get("status") == "shipped", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("幂等出货确认后库存 7", st1 == 7, f"stock={st1}")
        s, d = req("POST", "/api/transfers",
                   {"wine_id": wid, "from_warehouse_id": wh1,
                    "to_warehouse_id": wh2, "qty": 1,
                    "idempotency_key": "idem-tr-1"}, token=tok)
        idem_tr_no = d.get("transfer_no")
        check("幂等调拨第一次成功", s == 200 and idem_tr_no, f"status={s} {d}")
        s, d2 = req("POST", "/api/transfers",
                    {"wine_id": wid, "from_warehouse_id": wh1,
                     "to_warehouse_id": wh2, "qty": 1,
                     "idempotency_key": "idem-tr-1"}, token=tok)
        check("幂等调拨第二次返回原单",
              s == 200 and d2.get("transfer_no") == idem_tr_no
              and d2.get("duplicate") is True, f"status={s} {d2}")
        s, st = req("GET", "/api/stock", token=tok)
        m = {(x["warehouse_id"], x["wine_id"]): x["quantity"] for x in st}
        check("幂等调拨重复提交未调拨 (主仓 5/副仓 2)",
              m.get((wh1, wid)) == 5 and m.get((wh2, wid)) == 2, str(m))
        s, ds = req("GET", "/api/transfers", token=tok)
        idem_tr_id = [x for x in ds if x["transfer_no"] == idem_tr_no][0]["id"]
        s, d = req("POST", f"/api/transfers/{idem_tr_id}/confirm", {}, token=tok)
        check("幂等调拨确认一次后主仓 4/副仓 3",
              s == 200 and d.get("status") == "done", f"status={s} {d}")
        s, st = req("GET", "/api/stock", token=tok)
        m = {(x["warehouse_id"], x["wine_id"]): x["quantity"] for x in st}
        check("幂等调拨确认后主仓 4/副仓 3",
              m.get((wh1, wid)) == 4 and m.get((wh2, wid)) == 3, str(m))

        # 21e. 废单 (新语义): 没确认过可整单废, 全部确认完不可废,
        # 部分确认过可废剩余 (单据变已关闭)
        s, db = req("GET", "/api/dashboard", token=tok)
        check("废单前供应商欠款 1110",
              db.get("supplier_debt") == 1110, f"supplier_debt={db.get('supplier_debt')}")
        # 21e1. 待到货的单整单废
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 1, "unit_price": 120}]},
                   token=tok)
        pend_in = d.get("id")
        s, d = req("POST", f"/api/orders/inbound/{pend_in}/void", {}, token=tok)
        check("待到货的进货单整单废成功",
              s == 200 and d.get("status") == "voided", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("废待到货单后库存仍为 7 (主仓 4 + 副仓 3)", st1 == 7, f"stock={st1}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("废待到货单后供应商欠款仍为 1110",
              db.get("supplier_debt") == 1110, str(db))
        s, d = req("POST", f"/api/orders/inbound/{pend_in}/void", {}, token=tok)
        check("重复废单被拒 400", s == 400, f"status={s} {d}")
        # 21e2. 全部确认完的单不可废
        s, d = req("POST", f"/api/orders/inbound/{in_id}/void", {}, token=tok)
        check("已入库的进货单废单被拒 400", s == 400, f"status={s} {d}")
        # 21e3. 部分确认后废剩余
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 4, "unit_price": 120}]},
                   token=tok)
        part_in = d.get("id")
        s, d = req("POST", f"/api/inbound/{part_in}/confirm",
                   {"items": [{"item_id": req("GET", f"/api/inbound/{part_in}",
                                              token=tok)[1]["items"][0]["id"],
                               "qty": 1}]}, token=tok)
        check("部分确认 1 瓶后状态为部分入库",
              s == 200 and d.get("status") == "partial", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("部分确认后库存 8 (主仓 5 + 副仓 3)", st1 == 8, f"stock={st1}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("部分确认后供应商欠款 1230",
              db.get("supplier_debt") == 1230, str(db))
        s, d = req("POST", f"/api/orders/inbound/{part_in}/void", {}, token=tok)
        check("部分确认后废剩余成功, 单据变已关闭",
              s == 200 and d.get("status") == "closed", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("废剩余后库存仍为 8 (已确认的保留)", st1 == 8, f"stock={st1}")
        s, d = req("POST", f"/api/inbound/{part_in}/confirm", {}, token=tok)
        check("已关闭的单再确认被拒 400", s == 400, f"status={s} {d}")
        s, d = req("POST", f"/api/orders/inbound/{part_in}/void", {}, token=tok)
        check("已关闭的单再废被拒 400", s == 400, f"status={s} {d}")
        s, d = req("POST", "/api/orders/inbound/99999/void", {}, token=tok)
        check("废不存在的单 404", s == 404, f"status={s} {d}")
        s, d = req("POST", "/api/orders/xx/1/void", {}, token=tok)
        check("废单 kind 非法 400", s == 400, f"status={s} {d}")

        # 21f. 废销售单 (新语义)
        s, db = req("GET", "/api/dashboard", token=tok)
        check("废单前客户欠款 840",
              db.get("customer_debt") == 840, f"customer_debt={db.get('customer_debt')}")
        # 21f1. 待出货的单整单废
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 1, "unit_price": 200}]},
                   token=tok)
        pend_out = d.get("id")
        s, d = req("POST", f"/api/orders/outbound/{pend_out}/void", {}, token=tok)
        check("待出货的销售单整单废成功",
              s == 200 and d.get("status") == "voided", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("废待出货单后库存仍为 8", st1 == 8, f"stock={st1}")
        # 21f2. 全部出货完的单不可废
        s, d = req("POST", f"/api/orders/outbound/{disc_id}/void", {}, token=tok)
        check("已出货的销售单废单被拒 400", s == 400, f"status={s} {d}")
        # 21f3. 部分出货后废剩余
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 2, "unit_price": 200}]},
                   token=tok)
        part_out = d.get("id")
        s, d = req("POST", f"/api/outbound/{part_out}/confirm",
                   {"items": [{"item_id": req("GET", f"/api/outbound/{part_out}",
                                              token=tok)[1]["items"][0]["id"],
                               "qty": 1}]}, token=tok)
        check("部分出货 1 瓶后状态为部分出货",
              s == 200 and d.get("status") == "partial", f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        st1 = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("部分出货后库存 7 (主仓 4 + 副仓 3)", st1 == 7, f"stock={st1}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("部分出货后客户欠款 1040",
              db.get("customer_debt") == 1040, str(db))
        s, d = req("POST", f"/api/orders/outbound/{part_out}/void", {}, token=tok)
        check("部分出货后废剩余成功, 单据变已关闭",
              s == 200 and d.get("status") == "closed", f"status={s} {d}")
        s, d = req("POST", f"/api/outbound/{part_out}/confirm", {}, token=tok)
        check("已关闭的销售单再确认被拒 400", s == 400, f"status={s} {d}")
        s, d = req("POST", f"/api/orders/outbound/{pend_out}/payments",
                   {"amount": 10}, token=tok)
        check("已废单补记收款被拒 400", s == 400, f"status={s} {d}")
        s, d = req("GET", f"/api/orders/outbound/{pend_out}/share.png", token=tok)
        check("已废单分享图被拒 400", s == 400, f"status={s} {d}")
        s, ds = req("GET", "/api/outbound", token=tok)
        row = [o for o in ds if o["id"] == pend_out][0]
        check("废单后单据保留并标记 voided", row.get("voided") == 1, f"{row}")

        # 21g. 废调拨 (新语义): 确认前可废, 确认后不可废
        s, d = req("POST", "/api/transfers",
                   {"wine_id": wid, "from_warehouse_id": wh1,
                    "to_warehouse_id": wh2, "qty": 1}, token=tok)
        s, ds = req("GET", "/api/transfers", token=tok)
        pend_tr = [x for x in ds if x["transfer_no"] == d.get("transfer_no")][0]["id"]
        s, d = req("POST", f"/api/orders/transfers/{pend_tr}/void", {}, token=tok)
        check("待调拨的单废单成功",
              s == 200 and d.get("status") == "voided", f"status={s} {d}")
        s, st = req("GET", "/api/stock", token=tok)
        m = {(x["warehouse_id"], x["wine_id"]): x["quantity"] for x in st}
        check("废待调拨单后主仓 4/副仓 3",
              m.get((wh1, wid)) == 4 and m.get((wh2, wid)) == 3, str(m))
        s, d = req("POST", f"/api/orders/transfers/{idem_tr_id}/void", {}, token=tok)
        check("已完成的调拨废单被拒 400", s == 400, f"status={s} {d}")

        # 21h. 补登页可查当前库存
        s, st = req("GET", "/api/stock", token=tok)
        m = {(x["warehouse_id"], x["wine_id"]): x["quantity"] for x in st}
        check("补登页可查到当前库存 (主仓 4 瓶)",
              m.get((wh1, wid)) == 4, str(m))

        # 21i. 前端默认值与新元素: 首页 HTML 里有数量默认 1、已收默认 0、
        # 多酒品明细区、核对摘要区、补登当前库存区、物流折叠区
        s, html = req("GET", "/")
        check("前端有出货/进货多酒品明细区与核对摘要",
              s == 200 and 'id="out-items"' in html and 'id="in-items"' in html
              and 'id="out-summary"' in html, f"status={s}")
        check("前端数量默认 1、已收已付默认 0",
              'id="op-qty" type="number" value="1"' in html
              and 'id="out-paid" type="number" step="0.01" value="0"' in html
              and 'id="in-paid" type="number" step="0.01" value="0"' in html,
              f"status={s}")
        check("前端补登有当前库存显示与原因快捷选项",
              'id="op-stock-now"' in html and 'id="op-reason"' in html
              and '期初初始化' in html, f"status={s}")
        check("前端进货物流信息默认折叠",
              '<details id="in-logi">' in html, f"status={s}")
        check("前端有基础资料/仓库/库存页与确认按钮",
              'id="page-master"' in html and 'id="page-warehouses"' in html
              and 'id="page-inventory"' in html and '确认到货' in html
              and '确认出货' in html and '确认调拨' in html, f"status={s}")
        check("前端按钮文字不变: 无“处理中”文字, 有下方加载提示条",
              '处理中' not in html and 'id="loading-bar"' in html
              and '加载中' in html, f"status={s}")

        # 21j. 用户信息无 display_name
        s, us = req("GET", "/api/users", token=tok)
        check("用户列表不再返回 display_name",
              s == 200 and all("display_name" not in u for u in us),
              f"status={s} {us}")

        # 22. v2.4.0: 仓库管理、库存页、确认守卫
        # 22a. 仓库新增/改名/删除
        s, d = req("POST", "/api/warehouses", {"name": "测试仓"}, token=tok)
        test_wh = d.get("id")
        check("新增仓库测试仓", s == 200 and test_wh, f"status={s} {d}")
        s, whs = req("GET", "/api/warehouses", token=tok)
        row = [w for w in whs if w["id"] == test_wh][0]
        check("仓库列表含库存总瓶数 (测试仓 0 瓶)",
              row.get("stock_total") == 0, f"status={s} {row}")
        s, _ = req("PUT", f"/api/warehouses/{test_wh}", {"name": "测试仓改名"},
                   token=tok)
        check("仓库改名", s == 200, f"status={s}")
        s, whs = req("GET", "/api/warehouses", token=tok)
        row = [w for w in whs if w["id"] == test_wh][0]
        check("改名后仓库名已更新", row["name"] == "测试仓改名", f"status={s} {row}")
        s, _ = req("DELETE", f"/api/warehouses/{test_wh}", None, token=tok)
        check("空仓库可删除", s == 200, f"status={s}")
        s, d = req("DELETE", f"/api/warehouses/{wh2}", None, token=tok)
        check("有库存的仓库拒删 400", s == 400, f"status={s} {d}")
        s, _ = req("DELETE", "/api/warehouses/99999", None, token=tok)
        check("删除不存在的仓库 404", s == 404, f"status={s}")
        s, _ = req("PUT", "/api/warehouses/99999", {"name": "x"}, token=tok)
        check("改名不存在的仓库 404", s == 404, f"status={s}")

        # 22b. 未确认的单子指着仓库时不能删
        s, d = req("POST", "/api/warehouses", {"name": "临时仓"}, token=tok)
        tmp_wh = d.get("id")
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": tmp_wh,
                    "items": [{"wine_id": wid2, "qty": 1, "unit_price": 50}]},
                   token=tok)
        tmp_in = d.get("id")
        check("临时仓里建一张待到货单", s == 200 and tmp_in, f"status={s} {d}")
        s, d = req("DELETE", f"/api/warehouses/{tmp_wh}", None, token=tok)
        check("有未确认单据的仓库拒删 400", s == 400, f"status={s} {d}")
        s, d = req("POST", f"/api/orders/inbound/{tmp_in}/void", {}, token=tok)
        check("废掉临时仓的待到货单", s == 200, f"status={s} {d}")
        s, _ = req("DELETE", f"/api/warehouses/{tmp_wh}", None, token=tok)
        check("废单后仓库可删除", s == 200, f"status={s}")

        # 22c. 库存页接口: 分仓库返回酒、数量、成本
        s, d = req("GET", "/api/inventory", token=tok)
        check("库存页接口返回分仓库结构",
              s == 200 and "warehouses" in d, f"status={s}")
        wh1_inv = [w for w in d["warehouses"] if w["id"] == wh1][0]
        wh1_wid = [i for i in wh1_inv["items"] if i["wine_id"] == wid][0]
        check("库存页主仓测试红酒 4 瓶/成本 480",
              wh1_wid["qty"] == 4 and wh1_wid["cost"] == 480,
              f"status={s} {wh1_inv}")
        check("库存页主仓总数 11 瓶 (红酒 4 + 白酒 7)",
              wh1_inv["total_qty"] == 11, f"status={s} {wh1_inv}")
        wh2_inv = [w for w in d["warehouses"] if w["id"] == wh2][0]
        check("库存页副仓测试红酒 3 瓶",
              [i for i in wh2_inv["items"] if i["wine_id"] == wid][0]["qty"] == 3,
              f"status={s} {wh2_inv}")

        # 22d. 确认守卫: 超量确认、已确认完再确认、出货库存不足
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 2, "unit_price": 120}]},
                   token=tok)
        guard_in = d.get("id")
        s, d = req("GET", f"/api/inbound/{guard_in}", token=tok)
        g_item = d["items"][0]["id"]
        s, d = req("POST", f"/api/inbound/{guard_in}/confirm",
                   {"items": [{"item_id": g_item, "qty": 3}]}, token=tok)
        check("确认数量超过剩余被拒 400", s == 400, f"status={s} {d}")
        s, d = req("POST", f"/api/inbound/{guard_in}/confirm",
                   {"items": [{"item_id": g_item, "qty": 2}]}, token=tok)
        check("确认 2 瓶后已入库", s == 200 and d.get("status") == "received",
              f"status={s} {d}")
        s, d = req("POST", f"/api/inbound/{guard_in}/confirm",
                   {"items": [{"item_id": g_item, "qty": 1}]}, token=tok)
        check("已入库再确认被拒 400", s == 400, f"status={s} {d}")
        s, d = req("POST", f"/api/orders/inbound/{guard_in}/void", {}, token=tok)
        check("已入库废单被拒 400", s == 400, f"status={s} {d}")
        # 出货确认时库存不足被拒: 另建一款酒, 补登 5 瓶, 下 5 瓶销售单,
        # 再自饮 5 瓶把库存清掉, 确认出货时应被拒
        s, w3 = req("POST", "/api/wines",
                    {"barcode": "6901234567892", "name": "测试黄酒",
                     "brand": "测试牌", "spec": "500ml"}, token=tok)
        wid3 = w3.get("id")
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid3, "qty": 5, "unit_price": 60}]},
                   token=tok)
        check("第三款酒补登 5 瓶", s == 200, f"status={s} {d}")
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid3, "qty": 5, "unit_price": 100}]},
                   token=tok)
        guard_out = d.get("id")
        s, d = req("POST", "/api/outbound",
                   {"type": "personal", "warehouse_id": wh1,
                    "items": [{"wine_id": wid3, "qty": 5}]}, token=tok)
        check("自饮清掉第三款酒库存", s == 200, f"status={s} {d}")
        s, d = req("POST", f"/api/outbound/{guard_out}/confirm", {}, token=tok)
        check("确认出货时库存不足被拒 400", s == 400, f"status={s} {d}")

        # 22e. 欠款只算已确认部分: 新建待到货单不改变供应商欠款
        s, db = req("GET", "/api/dashboard", token=tok)
        debt_before = db.get("supplier_debt")
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 10, "unit_price": 120}]},
                   token=tok)
        check("新建待到货大单成功", s == 200, f"status={s} {d}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("待到货单不改变供应商欠款",
              db.get("supplier_debt") == debt_before, str(db))
        s, d = req("POST", f"/api/orders/inbound/{d.get('id')}/void", {}, token=tok)
        check("废掉该待到货单", s == 200, f"status={s} {d}")

        # 22f. 不能删最后一个仓库: 先把副仓库存调回主仓并删掉副仓
        s, d = req("POST", "/api/transfers",
                   {"wine_id": wid, "from_warehouse_id": wh2,
                    "to_warehouse_id": wh1, "qty": 3}, token=tok)
        s, ds = req("GET", "/api/transfers", token=tok)
        last_tr = [x for x in ds if x["transfer_no"] == d.get("transfer_no")][0]["id"]
        s, d = req("POST", f"/api/transfers/{last_tr}/confirm", {}, token=tok)
        check("副仓 3 瓶调回主仓", s == 200, f"status={s} {d}")
        s, _ = req("DELETE", f"/api/warehouses/{wh2}", None, token=tok)
        check("副仓清空后可删除", s == 200, f"status={s}")
        s, d = req("DELETE", f"/api/warehouses/{wh1}", None, token=tok)
        check("删除最后一个仓库被拒 400", s == 400, f"status={s} {d}")

        # 14. audit_logs 有记录
        s, logs = req("GET", "/api/audit-logs", token=tok)
        acts = {l["action"] for l in logs} if isinstance(logs, list) else set()
        need = {"登录", "进货", "出货", "补登期初", "调拨下单", "确认调拨", "收款", "新增管理员",
                "自饮", "赠送", "破损"}
        check("audit_logs 有关键操作记录", need <= acts,
              f"缺: {need - acts} / 共有 {len(acts)} 种")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    nfail = sum(1 for _, ok, _ in RESULTS if not ok)
    print(f"\n共 {len(RESULTS)} 项, PASS {len(RESULTS)-nfail}, FAIL {nfail}")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
