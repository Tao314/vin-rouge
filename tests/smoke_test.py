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
from datetime import date
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

        # 1b. 版本接口公开返回 v2.10.0（期望值写死在测试里，运行时版本以 app.py 的 APP_VERSION 为准）
        s, d = req("GET", "/api/version")
        check("版本接口 /api/version 返回 v2.10.0", s == 200 and d.get("version") == "v2.10.0", f"status={s} {d}")
        s, d = req("GET", "/api/config")
        check("配置接口 /api/config 版本一致", s == 200 and d.get("version") == "v2.10.0" and "build_date" in d, f"status={s} {d}")

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
        s, ds = req("GET", "/api/transfers", token=tok)
        _tr = [x for x in ds if x["id"] == tr_id][0]
        check("调拨单返回调出/调入仓库名",
              _tr.get("from_warehouse_name") and _tr.get("to_warehouse_name")
              and _tr["from_warehouse_name"] != _tr["to_warehouse_name"],
              f"{_tr}")
        s, _html = req("GET", "/")
        check("调拨单详情前端有调出调入仓库名拼接",
              "from_warehouse_name" in _html and "to_warehouse_name" in _html
              and "from_warehouse_name||''} → ${o.to_warehouse_name" in _html,
              f"status={s}")
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
                   {"amount": 610}, token=tok)
        check("补记收款 610（货款 600 + 客户付运费 10）后状态变已付",
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

        # 20. 分享图: 折扣销售单有图, 赠送单被拒; 打折无原因可提交
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
        disc_nr_id = d.get("id")
        check("打折无原因可提交 (折后 180)", s == 200 and disc_nr_id
              and d.get("sale_total") == 180.0 and d.get("discount_total") == 20.0,
              f"status={s} {d}")
        s, d = req("POST", f"/api/orders/outbound/{disc_nr_id}/void", {},
                   token=tok)
        check("无原因折扣单废单清理", s == 200, f"status={s} {d}")

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
        check("前端有管理页/仓库/库存页与确认按钮",
              'id="page-admin"' in html and 'id="page-warehouses"' in html
              and 'id="page-inventory"' in html and '确认到货' in html
              and '确认出货' in html and '确认调拨' in html, f"status={s}")
        # 21k. v2.5.0: 导航简化 + 调拨单详情补全
        import re as _re
        nav = _re.search(r'<div class="nav">(.*?)</div>', html, _re.S).group(1)
        nav_labels = _re.findall(r'>([^<]+)</button>', nav)
        check("顶栏只留七个导航: 首页/进货/出货/调拨/库存/单据/管理",
              nav_labels == ["首页", "进货", "出货", "调拨", "库存", "单据", "管理"],
              f"nav={nav_labels}")
        admin = _re.search(r'id="page-admin"(.*?)</div></div>', html, _re.S).group(1)
        admin_labels = _re.findall(r'>([^<]+)</button>', admin)
        check("管理页有八个二级入口: 酒库/补登/供应商/客户/仓库/操作记录/用户管理/改密码",
              admin_labels == ["酒库", "补登", "供应商", "客户", "仓库", "操作记录", "用户管理", "改密码"],
              f"admin={admin_labels}")
        check("基础资料页已无入口",
              'page-master' not in html and '基础资料' not in html
              and "show('master')" not in html, f"status={s}")
        check("前端按钮文字不变: 无“处理中”文字, 有下方加载提示条",
              '处理中' not in html and 'id="loading-bar"' in html
              and '加载中' in html, f"status={s}")
        # 21l. v2.5.1: 全站按钮样式统一
        css = _re.search(r'<style>(.*?)</style>', html, _re.S).group(1)
        nav_css = _re.search(r'\.nav\{([^}]*)\}', css).group(1)
        nav_btn_rule = _re.search(r'\.nav button\{([^}]*)\}', css).group(1)
        check("导航用 5 列等宽网格, 按钮不再 flex 拉宽",
              'display:grid' in nav_css
              and 'grid-template-columns:repeat(5,1fr)' in nav_css
              and 'flex' not in nav_btn_rule, f"nav={nav_css} btn={nav_btn_rule}")
        ghost_rule = _re.search(r'button\.ghost\{([^}]*)\}', css).group(1)
        check("次要按钮统一白底酒红边框红字, 无灰底",
              'background:#fff' in ghost_rule and 'color:#7b1e26' in ghost_rule
              and 'border:1px solid #7b1e26' in ghost_rule
              and '#F2F3F5' not in css, f"ghost={ghost_rule}")
        check("按钮组都用等宽网格: 管理页 g4 / 单据切换 g2 + 类型筛选 g4x2 / 付款快捷 g3 / 补登原因 g2",
              html.count('class="btn-grid g4"') == 1
              and html.count('btn-grid g4 hidden') == 2
              and html.count('class="btn-grid g5"') == 0
              and html.count('class="btn-grid g3"') == 2
              and html.count('class="btn-grid g2"') == 2, f"status={s}")
        btn_classes = sorted(set(_re.findall(r'<button[^>]*class="([^"]*)"', html)))
        check("全站按钮 class 只有统一的几种, 无孤立样式",
              set(btn_classes) <= {"ghost", "ghost hidden", "hidden", "btn-block",
                                   "ghost btn-block", "ghost btn-block hidden"},
              f"classes={btn_classes}")
        check("表单主按钮统一全宽, 保存+取消成对等宽",
              html.count('class="btn-block"') >= 7
              and html.count('class="btn-pair"') == 3, f"status={s}")
        # 21m. v2.5.2: 按钮文字不许换行
        btn_rule = _re.search(r'(?:^|\n)button\{([^}]*)\}', css).group(1)
        grid_btn_rule = _re.search(r'\.btn-grid button\{([^}]*)\}', css).group(1)
        check("按钮文字不许换行: 全站 button 有 white-space:nowrap",
              'white-space:nowrap' in btn_rule, f"button={btn_rule}")
        check("管理页按钮组是 4 列等宽网格, 网格按钮横向内边距已减小",
              '.btn-grid.g4{grid-template-columns:repeat(4,1fr)}' in css
              and html.count('class="btn-grid g4"') == 1
              and 'padding:9px 4px' in grid_btn_rule, f"grid_btn={grid_btn_rule}")

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

        # 23. v2.6.0: 改密码入口、酒售价+按行折扣、表单不超宽、单据页改版
        # 23a. 酒的默认售价: 建酒可填售价, 列表带回, 修改生效, 不填则为空
        s, w = req("POST", "/api/wines",
                   {"barcode": "6901234567893", "name": "测试售价酒",
                    "capacity_ml": 750, "sale_price": 268}, token=tok)
        wid_sp = w.get("id")
        check("建酒可填默认售价 268", s == 200 and wid_sp, f"status={s} {w}")
        s, ws = req("GET", "/api/wines", token=tok)
        row = [x for x in ws if x["id"] == wid_sp][0]
        check("酒列表带回售价 268", row.get("sale_price") == 268, f"status={s} {row}")
        s, _ = req("PUT", f"/api/wines/{wid_sp}", {"sale_price": 288}, token=tok)
        s, ws = req("GET", "/api/wines", token=tok)
        row = [x for x in ws if x["id"] == wid_sp][0]
        check("酒售价可修改为 288", s == 200 and row.get("sale_price") == 288,
              f"status={s} {row}")
        s, w = req("POST", "/api/wines",
                   {"barcode": "6901234567894", "name": "测试无售价酒",
                    "capacity_ml": 500}, token=tok)
        wid_nosp = w.get("id")
        s, ws = req("GET", "/api/wines", token=tok)
        row = [x for x in ws if x["id"] == wid_nosp][0]
        check("不填售价的酒 sale_price 为空", row.get("sale_price") is None,
              f"status={s} {row}")

        # 23b. 按行折扣: 三行各不同折扣, 汇总按行算
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid_sp, "qty": 5, "unit_price": 120},
                              {"wine_id": wid_nosp, "qty": 5, "unit_price": 60}]},
                   token=tok)
        check("折扣测试酒各补登 5 瓶", s == 200, f"status={s} {d}")
        s, d = req("POST", "/api/outbound",
                   {"type": "sale", "customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_sp, "qty": 2, "orig_price": 288,
                               "discount_rate": 0.9},
                              {"wine_id": wid_nosp, "qty": 3, "orig_price": 100,
                               "discount_rate": 0.8},
                              {"wine_id": wid, "qty": 1, "orig_price": 150}]},
                   token=tok)
        disc3_id = d.get("id")
        check("三行不同折扣: 原价 1026/实收 908.4/折扣 117.6",
              s == 200 and d.get("orig_total") == 1026
              and d.get("sale_total") == 908.4 and d.get("discount_total") == 117.6,
              f"status={s} {d}")
        s, d = req("GET", f"/api/outbound/{disc3_id}", token=tok)
        its = {i["wine_id"]: i for i in d["items"]}
        check("每行折扣额按行存 (无原因也能打折)",
              its[wid_sp]["discount_amount"] == 57.6
              and its[wid_nosp]["discount_amount"] == 60
              and its[wid]["discount_amount"] == 0, f"status={s} {d}")
        s, d = req("POST", "/api/outbound",
                   {"type": "sale", "customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_sp, "qty": 1, "orig_price": 288,
                               "discount_rate": 0.9}]}, token=tok)
        disc_noreason_id = d.get("id")
        check("打折无原因可提交 (折后价 259.2 按折扣率算)",
              s == 200 and disc_noreason_id and d.get("sale_total") == 259.2
              and d.get("discount_total") == 28.8, f"status={s} {d}")
        s, d = req("POST", f"/api/orders/outbound/{disc_noreason_id}/void", {},
                   token=tok)
        check("无原因折扣单废单清理", s == 200, f"status={s} {d}")
        s, d = req("POST", f"/api/outbound/{disc3_id}/confirm", {}, token=tok)
        check("确认三行折扣单后已出货",
              s == 200 and d.get("status") == "shipped", f"status={s} {d}")
        s, d = req("GET", f"/api/orders/outbound/{disc3_id}/share.png", token=tok)
        check("按行折扣单分享图仍为有效 PNG", s == 200 and d[:8] == b"\x89PNG\r\n\x1a\n",
              f"status={s}")

        # 23c. 改密码入口: 旧密码错被拒, 改对后能登录 (主管理员测, 改完改回;
        # 注: admin2 在前面已被停用, 其 token 已失效, 不能用它测)
        s, d = req("POST", "/api/change-password",
                   {"old_password": "WrongPass", "new_password": "TempPass123"},
                   token=tok)
        check("原密码错误改密被拒", s == 400, f"status={s} {d}")
        s, d = req("POST", "/api/change-password",
                   {"old_password": "NewPass123", "new_password": "TempPass123"},
                   token=tok)
        check("管理员改密成功", s == 200, f"status={s} {d}")
        s, d2x = req("POST", "/api/login",
                     {"username": "tristan", "password": "TempPass123"})
        check("改密后新密码能登录", s == 200 and "token" in d2x, f"status={s}")
        s, d = req("POST", "/api/change-password",
                   {"old_password": "TempPass123", "new_password": "NewPass123"},
                   token=d2x.get("token", ""))
        check("改密后改回原密码", s == 200, f"status={s} {d}")
        # 登录会发新 token, 旧 tok 已失效, 重新登录拿新 token 供后面用
        s, d = req("POST", "/api/login",
                   {"username": "tristan", "password": "NewPass123"})
        tok = d.get("token", "")
        check("改回后重新登录拿新 token", s == 200 and bool(tok), f"status={s}")

        # 23d. 单据列表接口返回明细 (卡片直接渲染用)
        s, ds = req("GET", "/api/outbound", token=tok)
        row = [x for x in ds if x["id"] == disc3_id][0]
        check("出货列表带明细 items (3 行)",
              "items" in row and len(row["items"]) == 3
              and row["items"][0].get("wine_name"), f"status={s}")
        s, ds = req("GET", "/api/inbound", token=tok)
        row = [x for x in ds if x["id"] == opening_id][0]
        check("进货列表带明细 items",
              "items" in row and len(row["items"]) >= 1
              and row["items"][0].get("wine_name"), f"status={s}")

        # 23e. 前端 v2.6.0: 改密码入口、售价输入、按行折扣、表单不超宽、单据页改版
        s, html = req("GET", "/")
        check("管理页有改密码入口, 改密卡片有取消返回",
              "showChpw()" in html and 'id="chpw-cancel"' in html
              and 'id="chpw-title"' in html, f"status={s}")
        check("酒库表单有默认售价输入, 出货行有按行折扣输入",
              'id="w-sale-price"' in html and 'outLineRate(' in html
              and 'outLineDprice(' not in html and 'outLineReason(' not in html
              and '折扣原因' not in html, f"status={s}")
        check("整单级折扣输入已去掉",
              'id="out-rate"' not in html and 'id="out-reason"' not in html
              and 'id="out-discount"' not in html, f"status={s}")
        css = _re.search(r'<style>(.*?)</style>', html, _re.S).group(1)
        check("表单输入框不超宽: max-width:100% 且 flex 子项可收缩",
              'max-width:100%' in css
              and '.line select{flex:2;min-width:0' in css
              and '.line input{flex:1;min-width:0' in css, f"status={s}")
        check("单据页无“单据”标题、无详情按钮、无详情页",
              '<h2>单据</h2>' not in html and 'showOrder(' not in html
              and 'showTransfer(' not in html and 'order-detail' not in html
              and '详情' not in html, f"status={s}")
        check("单据页直接渲染完整卡片, 付款状态显示中文",
              'orderCard(' in html and 'order-card-' in html
              and '已付清' in html and '部分付' in html and '未付' in html
              and 'pay-amt' in html, f"status={s}")

        # 24. v2.7.0 运费新规则
        # 24a. 隔离客户, 避免影响已有欠款断言
        s, d = req("POST", "/api/customers",
                   {"name": "运费测试客户", "phone": "13600000000"}, token=tok)
        cus2 = d.get("id")
        check("新建运费测试客户", s == 200 and cus2, f"status={s} {d}")

        def cus2_bal():
            s, cuss = req("GET", "/api/customers", token=tok)
            return [c["balance"] for c in cuss if c["id"] == cus2][0]

        # 24b. 进货单: 客户端仍发 freight_free/freight_payer 也被后端强制改写
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "freight": 30, "freight_free": True, "freight_payer": "供应商",
                    "items": [{"wine_id": wid, "qty": 1, "unit_price": 100}]},
                   token=tok)
        in_f = d.get("id")
        check("进货单下单成功", s == 200 and in_f, f"status={s} {d}")
        s, d = req("GET", f"/api/inbound/{in_f}", token=tok)
        check("进货单运费仍为实际金额 30, 付方强制为我方",
              d.get("freight") == 30 and d.get("freight_payer") == "我方"
              and not d.get("freight_free"), str(d))

        # 24c. 出货包邮: 运费仍是实际金额, 付方我方, 欠款只含货款
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus2, "warehouse_id": wh1, "freight": 25,
                    "freight_free": True,
                    "items": [{"wine_id": wid, "qty": 1, "unit_price": 200}]},
                   token=tok)
        out_free_id = d.get("id")
        check("包邮出货单下单成功", s == 200 and out_free_id, f"status={s} {d}")
        s, d = req("GET", f"/api/outbound/{out_free_id}", token=tok)
        check("包邮出货单运费仍为实际金额 25, 付方为我方, 新规则标记为 1",
              d.get("freight") == 25 and d.get("freight_payer") == "我方"
              and d.get("freight_rule") == 1, str(d))
        s, d = req("POST", f"/api/outbound/{out_free_id}/confirm", {}, token=tok)
        check("包邮出货确认后状态为已出货",
              s == 200 and d.get("status") == "shipped", f"status={s} {d}")
        b = cus2_bal()
        check("包邮出货确认后客户欠款 200 (只含货款, 不含运费)", b == 200, f"bal={b}")
        s, d = req("POST", f"/api/orders/outbound/{out_free_id}/payments",
                   {"amount": 200}, token=tok)
        check("包邮单付 200 后已付清",
              s == 200 and d.get("pay_status") == "paid", f"status={s} {d}")
        b = cus2_bal()
        check("包邮单付清后客户欠款 0", b == 0, f"bal={b}")

        # 24d. 出货不包邮: 付方客户, 运费首次确认即计入欠款一次, 分批不再重复加
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus2, "warehouse_id": wh1, "freight": 20,
                    "freight_free": False,
                    "items": [{"wine_id": wid, "qty": 2, "unit_price": 200}]},
                   token=tok)
        out_pay_id = d.get("id")
        check("不包邮出货单下单成功", s == 200 and out_pay_id, f"status={s} {d}")
        s, d = req("GET", f"/api/outbound/{out_pay_id}", token=tok)
        check("不包邮出货单运费为实际金额 20, 付方为客户",
              d.get("freight") == 20 and d.get("freight_payer") == "客户"
              and d.get("freight_rule") == 1, str(d))
        item0 = d["items"][0]["id"]
        s, d = req("POST", f"/api/outbound/{out_pay_id}/confirm",
                   {"items": [{"item_id": item0, "qty": 1}]}, token=tok)
        check("部分出货 1 瓶后状态为部分出货",
              s == 200 and d.get("status") == "partial", f"status={s} {d}")
        b = cus2_bal()
        check("首次确认时运费已计入欠款: 部分确认后欠款 220 (货款 200 + 运费 20)",
              b == 220, f"bal={b}")
        s, d = req("POST", f"/api/outbound/{out_pay_id}/confirm", {}, token=tok)
        check("剩余确认后状态为已出货",
              s == 200 and d.get("status") == "shipped", f"status={s} {d}")
        b = cus2_bal()
        check("全部确认后欠款 420 (货款 400 + 运费 20, 运费未重复加)",
              b == 420, f"bal={b}")
        # 24d2. 老单保持原规则: 把标记改回 0 模拟 v2.7.0 之前的单
        import sqlite3 as _sq
        _con = _sq.connect(os.path.join(tmp, "test.db"))
        _con.execute("UPDATE outbound_orders SET freight_rule=0 WHERE id=?",
                     (out_pay_id,))
        _con.commit()
        _con.close()
        s, d = req("GET", f"/api/outbound/{out_pay_id}", token=tok)
        check("老单运费付方和金额保持原样",
              d.get("freight_payer") == "客户" and d.get("freight") == 20, str(d))
        b = cus2_bal()
        check("老单欠款按原规则不含运费: 400", b == 400, f"bal={b}")
        s, d = req("POST", f"/api/orders/outbound/{out_pay_id}/payments",
                   {"amount": 400}, token=tok)
        check("老单付 400 后已付清 (按原规则总金额 400)",
              s == 200 and d.get("pay_status") == "paid", f"status={s} {d}")
        b = cus2_bal()
        check("老单付清后客户欠款 0", b == 0, f"bal={b}")

        # 24e. 利润扣运费: 专用酒成本 100, 卖 300
        s, w = req("POST", "/api/wines",
                   {"barcode": "6901234567895", "name": "利润测试酒",
                    "brand": "测试牌", "spec": "750ml"}, token=tok)
        wid2 = w.get("id")
        check("建利润测试酒", s == 200 and wid2, f"status={s} {w}")
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid2, "qty": 5, "unit_price": 100}]},
                   token=tok)
        check("利润测试酒补登 5 瓶成本 100", s == 200, f"status={s} {d}")
        s, db = req("GET", "/api/dashboard", token=tok)
        p0 = db.get("today_profit")
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus2, "warehouse_id": wh1, "freight": 25,
                    "freight_free": True,
                    "items": [{"wine_id": wid2, "qty": 1, "unit_price": 300}]},
                   token=tok)
        out_p1 = d.get("id")
        s, d = req("POST", f"/api/outbound/{out_p1}/confirm", {}, token=tok)
        check("包邮利润单确认成功", s == 200, f"status={s} {d}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("包邮单今日毛利增量 175 ((300-100)-25)",
              round(db.get("today_profit") - p0, 2) == 175,
              f"p0={p0} p1={db.get('today_profit')}")
        p0 = db.get("today_profit")
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus2, "warehouse_id": wh1, "freight": 20,
                    "freight_free": False,
                    "items": [{"wine_id": wid2, "qty": 1, "unit_price": 300}]},
                   token=tok)
        out_p2 = d.get("id")
        s, d = req("POST", f"/api/outbound/{out_p2}/confirm", {}, token=tok)
        check("客户付运费单确认成功", s == 200, f"status={s} {d}")
        s, db = req("GET", "/api/dashboard", token=tok)
        check("客户付运费单今日毛利增量 200 (运费收支抵消)",
              round(db.get("today_profit") - p0, 2) == 200,
              f"p0={p0} p1={db.get('today_profit')}")

        # 24f. 前端: 无重复大标题, 运费标签与提示为新文案
        s, html = req("GET", "/")
        check("各页无重复大标题 (登录/注册/改密标题保留)",
              s == 200
              and "<h2>酒库</h2>" not in html and "<h2>供应商</h2>" not in html
              and "<h2>客户</h2>" not in html and "<h2>进货单</h2>" not in html
              and "<h2>库存补登" not in html and "<h2>出货单</h2>" not in html
              and "<h2>调拨</h2>" not in html and "<h2>管理</h2>" not in html
              and "<h2>仓库管理</h2>" not in html and "<h2>库存</h2>" not in html
              and "<h2>用户管理" not in html and "<h2>操作记录</h2>" not in html
              and "<h2>登录</h2>" in html and "<h2>注册首位管理员</h2>" in html
              and 'id="chpw-title"' in html, f"status={s}")
        check("补登和用户管理说明小字还在",
              "增加库存模式" in html and "所有管理员权限相同" in html
              and "酒、补登、往来单位" in html, f"status={s}")
        check("进货单表单只有实际运费, 无包邮勾选和谁付栏",
              'id="in-freight"' in html and 'id="in-free"' not in html
              and 'id="in-payer"' not in html
              and "固定由我方承担，不计入供应商欠款；填 0 表示无运费。" in html
              and "实际运费" in html, f"status={s}")
        check("出货单表单有包邮和实际运费, 无谁付栏",
              'id="out-free"' in html and 'id="out-freight"' in html
              and 'id="out-payer"' not in html
              and "填写实际物流费用；包邮表示由我方承担，并非运费为 0。" in html
              and "我方付运费" in html, f"status={s}")
        check("前端运费分摊逻辑在场",
              "custFreight" in html and "客户付" in html and "我方付" in html
              and "运费记 0" not in html, f"status={s}")

        # 25. v2.8.0: 间距、酒编辑+价格历史、规格、品牌、折后价自动算、单据行快照
        # 25a. 前端间距: 单据筛选按钮组与卡片有间距; 行内按钮有间距
        s, html = req("GET", "/")
        check("单据筛选按钮组与卡片列表有间距",
              "#page-orders .btn-grid{margin-bottom:10px}" in html, f"status={s}")
        check("行内并排按钮有间距",
              ".item>div button{margin-left:6px}" in html, f"status={s}")

        # 25b. 酒编辑 + 价格历史 + 资料更新日期
        s, w = req("POST", "/api/wines",
                   {"barcode": "6901234567896", "name": "测试编辑酒",
                    "capacity_ml": 750, "sale_price": 100}, token=tok)
        wid_ed = w.get("id")
        check("建编辑测试酒", s == 200 and wid_ed, f"status={s} {w}")
        s, ws = req("GET", "/api/wines", token=tok)
        row = [x for x in ws if x["id"] == wid_ed][0]
        check("酒列表带回容量 750", row.get("capacity_ml") == 750,
              f"status={s} {row}")
        check("酒列表无 brand 字段", "brand" not in row, f"status={s} {row}")
        s, h = req("GET", f"/api/wines/{wid_ed}/price-history", token=tok)
        check("初始录入酒不记价格历史", s == 200 and h == [], f"status={s} {h}")
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid_ed, "qty": 5, "unit_price": 60}]},
                   token=tok)
        check("编辑测试酒补登 5 瓶", s == 200, f"status={s} {d}")
        s, d = req("POST", "/api/outbound",
                   {"type": "sale", "customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_ed, "qty": 1, "orig_price": 100}]},
                   token=tok)
        old_out_id = d.get("id")
        check("改价前出单成功", s == 200 and old_out_id, f"status={s} {d}")
        s, _ = req("PUT", f"/api/wines/{wid_ed}",
                   {"name": "测试编辑酒改名", "sale_price": 120}, token=tok)
        s, ws = req("GET", "/api/wines", token=tok)
        row = [x for x in ws if x["id"] == wid_ed][0]
        check("酒改名改价生效",
              s == 200 and row.get("name") == "测试编辑酒改名"
              and row.get("sale_price") == 120, f"status={s} {row}")
        check("酒卡片资料更新日期为今天",
              (row.get("updated_at") or "")[:10] == date.today().isoformat(),
              f"status={s} {row}")
        s, h = req("GET", f"/api/wines/{wid_ed}/price-history", token=tok)
        check("改价记一条价格历史 (100→120, 记操作人)",
              s == 200 and len(h) == 1 and h[0]["old_price"] == 100
              and h[0]["new_price"] == 120 and h[0]["changed_by"] == "tristan",
              f"status={s} {h}")
        s, d = req("GET", f"/api/outbound/{old_out_id}", token=tok)
        check("老单行价仍为 100 不动",
              d["items"][0]["unit_price"] == 100, f"status={s} {d}")
        check("老单行酒名快照仍为改名前",
              d["items"][0]["wine_name"] == "测试编辑酒", f"status={s} {d}")
        s, _ = req("PUT", f"/api/wines/{wid_ed}",
                   {"description": "只改说明不改价"}, token=tok)
        s, h = req("GET", f"/api/wines/{wid_ed}/price-history", token=tok)
        check("只改说明不改价不记价格历史", s == 200 and len(h) == 1,
              f"status={s} {h}")
        s, _ = req("PUT", f"/api/wines/{wid_ed}", {"sale_price": 120}, token=tok)
        s, h = req("GET", f"/api/wines/{wid_ed}/price-history", token=tok)
        check("改价为同样价格不记历史", s == 200 and len(h) == 1,
              f"status={s} {h}")
        s, d = req("POST", "/api/outbound",
                   {"type": "sale", "customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_ed, "qty": 1, "orig_price": 120}]},
                   token=tok)
        new_out_id = d.get("id")
        s, d = req("GET", f"/api/outbound/{new_out_id}", token=tok)
        check("新单行酒名快照为改名后",
              d["items"][0]["wine_name"] == "测试编辑酒改名", f"status={s} {d}")

        # 25c. 规格校验: 0/负数/小数/非数字被拒
        for _bad in (0, -100, 750.5, "abc"):
            s, d = req("POST", "/api/wines",
                       {"name": "坏规格酒", "capacity_ml": _bad}, token=tok)
            check(f"规格 {_bad} 被拒 400", s == 400, f"status={s} {d}")

        # 25d. 规格迁移: 示例酒容量已提为数字
        s, ws = req("GET", "/api/wines", token=tok)
        _seed = [x for x in ws if x["barcode"] == "6900000000001"]
        check("示例红酒迁移后容量 750",
              bool(_seed) and _seed[0].get("capacity_ml") == 750
              and _seed[0].get("spec") == "750", f"status={s} {_seed}")

        # 25e. 前端: 酒卡片有编辑入口与资料更新文案, 无品牌栏, 规格为单瓶容量
        s, html = req("GET", "/")
        check("酒卡片有编辑入口与资料更新文案",
              "editWine(" in html and "资料更新" in html
              and 'id="w-price-hist"' in html, f"status={s}")
        check("酒表单无品牌/厂家栏, 规格为单瓶容量数字输入",
              'id="w-brand"' not in html and 'id="w-manu"' not in html
              and "单瓶容量" in html and "品牌" not in html
              and 'id="w-spec" type="number"' in html, f"status={s}")
        check("折后价只读显示由折扣率算, 无折扣原因栏",
              'class="disc-price"' in html and "折后价 ¥" in html
              and 'outLineDprice(' not in html and 'outLineReason(' not in html
              and '折扣原因' not in html, f"status={s}")

        # 26. v2.9.0: 单据页分未结单 / 已结单
        # 前端 ordSettled/ordTypeOf 的同规则镜像 (见 static/index.html):
        # v2.10.0 双口径: 未结单 = 货没走完 或 欠款 > 0; 已结单 = 货走完 且 欠款 = 0;
        # 已作废永远进已结单, 已关闭按欠款算; 立即生效的单 (补登/自饮/赠送/破损)
        # 下单即已结; 欠款按卡片口径算, 比较前按分四舍五入.
        def _totals(o):
            q = sum(i["qty"] for i in o.get("items") or [])
            cq = sum(i.get("confirmed_qty") or 0 for i in o.get("items") or [])
            return q, cq

        def _owed(kind, o):
            # 卡片欠款口径的镜像 (与前端 ordOwed 一致): 已确认金额 + 应计运费
            # - 已退 - 已付, 按分四舍五入后下限为 0; 调拨/退货/非销售出库没有钱
            if kind in ("transfers", "returns"):
                return 0
            if kind == "outbound" and o.get("type") != "sale":
                return 0
            sale = o.get("type") == "sale"
            conf = sum((i.get("confirmed_qty") or 0) * (i.get("unit_price") or 0)
                       for i in o.get("items") or [])
            cq = sum(i.get("confirmed_qty") or 0 for i in o.get("items") or [])
            freight = (o.get("freight") or 0) if (sale and o.get("freight_rule")
                                                 and not o.get("freight_free")
                                                 and cq > 0) else 0
            tot = conf + freight - (o.get("returned_amount") or 0)
            return max(round(tot - (o.get("paid_amount") or 0), 2), 0)

        def _settled(kind, o):
            if o.get("voided"):
                return True
            if kind == "transfers":
                return o.get("status") == "done"
            if kind == "inbound" and o.get("type") == "opening":
                return True
            if kind == "outbound" and o.get("type") != "sale":
                return True
            if kind == "returns":
                q, cq = _totals(o)
                return o.get("status") == "closed" or (q > 0 and cq >= q)
            q, cq = _totals(o)
            goods_done = o.get("status") == "closed" or (q > 0 and cq >= q)
            return goods_done and _owed(kind, o) <= 0

        def _otype(kind, o):
            if kind == "transfers":
                return "transfer"
            if kind == "returns":
                return "outbound" if o.get("kind") == "sale" else "purchase"
            if kind == "inbound":
                return "opening" if o.get("type") == "opening" else "purchase"
            return "outbound"

        def _all_lists():
            _, ib = req("GET", "/api/inbound", token=tok)
            _, ob = req("GET", "/api/outbound", token=tok)
            _, tr = req("GET", "/api/transfers", token=tok)
            _, rt = req("GET", "/api/returns", token=tok)
            return {"inbound": ib, "outbound": ob, "transfers": tr,
                    "returns": rt}

        def _in_view(oid, kind, view, otype=""):
            for k, lst in _all_lists().items():
                for o in lst:
                    if o["id"] == oid and k == kind:
                        if ("closed" == view) != _settled(k, o):
                            return False
                        if view == "closed" and otype and _otype(k, o) != otype:
                            return False
                        return True
            return False

        # 26a. 新酒 + 补登 20 瓶作底; 补登单直接进已结单/补货单
        s, w = req("POST", "/api/wines",
                   {"barcode": "6901234567897", "name": "单据页测试酒",
                    "capacity_ml": 750, "sale_price": 200}, token=tok)
        wid_o = w.get("id")
        check("建单据页测试酒", s == 200 and wid_o, f"status={s} {w}")
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid_o, "qty": 20, "unit_price": 100}]},
                   token=tok)
        op26 = d.get("id")
        check("补登单直接进已结单/补货单",
              s == 200 and _in_view(op26, "inbound", "closed", "opening")
              and not _in_view(op26, "inbound", "open"), f"status={s} {d}")

        # 26b. 进货单: 未确认在未结单, 全部确认后进已结单/进货单
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_o, "qty": 2, "unit_price": 120}]},
                   token=tok)
        in26 = d.get("id")
        check("未确认进货单在未结单、不在已结单",
              s == 200 and _in_view(in26, "inbound", "open")
              and not _in_view(in26, "inbound", "closed"), f"status={s} {d}")
        s, d = req("POST", f"/api/inbound/{in26}/confirm", {}, token=tok)
        check("进货单确认成功", s == 200, f"status={s} {d}")
        check("全部确认未付款仍留在未结单 (双口径: 钱没付清)",
              _in_view(in26, "inbound", "open")
              and not _in_view(in26, "inbound", "closed"), f"status={s} {d}")
        s, d = req("POST", f"/api/orders/inbound/{in26}/payments",
                   {"amount": 240}, token=tok)
        check("进货单补记付款到付清", s == 200, f"status={s} {d}")
        check("付清后只在已结单/进货单",
              _in_view(in26, "inbound", "closed", "purchase")
              and not _in_view(in26, "inbound", "open")
              and not _in_view(in26, "inbound", "closed", "opening"),
              f"status={s} {d}")

        # 26c. 进货单部分确认后废剩余 -> 已关闭, 进已结单
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_o, "qty": 2, "unit_price": 120}]},
                   token=tok)
        in26b = d.get("id")
        s, d = req("GET", f"/api/inbound/{in26b}", token=tok)
        item26b = d["items"][0]["id"]
        s, d = req("POST", f"/api/inbound/{in26b}/confirm",
                   {"items": [{"item_id": item26b, "qty": 1}]}, token=tok)
        check("部分确认后仍在未结单",
              s == 200 and _in_view(in26b, "inbound", "open")
              and not _in_view(in26b, "inbound", "closed"), f"status={s} {d}")
        s, db1 = req("GET", "/api/dashboard", token=tok)
        sup_d1 = db1.get("supplier_debt")
        s, d = req("POST", f"/api/orders/inbound/{in26b}/void", {}, token=tok)
        check("部分确认后废剩余成功变已关闭",
              s == 200 and d.get("status") == "closed", f"status={s} {d}")
        check("废剩余后仍在未结单 (已确认部分有欠款)",
              _in_view(in26b, "inbound", "open")
              and not _in_view(in26b, "inbound", "closed"), f"status={s} {d}")
        s, db2 = req("GET", "/api/dashboard", token=tok)
        check("关闭后已确认部分的欠款保留 (废剩余前后供应商欠款不变)",
              db2.get("supplier_debt") == sup_d1,
              f"d1={sup_d1} d2={db2.get('supplier_debt')}")
        s, d = req("POST", f"/api/orders/inbound/{in26b}/payments",
                   {"amount": 120}, token=tok)
        check("关闭的单补记付款到付清", s == 200, f"status={s} {d}")
        check("付清后进已结单/进货单",
              _in_view(in26b, "inbound", "closed", "purchase")
              and not _in_view(in26b, "inbound", "open"), f"status={s} {d}")
        s, d = req("POST", f"/api/inbound/{in26b}/confirm", {}, token=tok)
        check("已关闭的单再确认被拒 400", s == 400, f"status={s} {d}")

        # 26d. 未确认进货单整单废 -> 已结单
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_o, "qty": 1, "unit_price": 120}]},
                   token=tok)
        in26c = d.get("id")
        s, d = req("POST", f"/api/orders/inbound/{in26c}/void", {}, token=tok)
        check("未确认废单成功",
              s == 200 and d.get("status") == "voided", f"status={s} {d}")
        check("未确认废单后进已结单",
              _in_view(in26c, "inbound", "closed", "purchase")
              and not _in_view(in26c, "inbound", "open"), f"status={s} {d}")

        # 26e. 出货单: 未确认在未结单, 确认后进已结单/出货单
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_o, "qty": 1, "unit_price": 200}]},
                   token=tok)
        out26 = d.get("id")
        check("未确认出货单在未结单、不在已结单",
              s == 200 and _in_view(out26, "outbound", "open")
              and not _in_view(out26, "outbound", "closed"), f"status={s} {d}")
        s, d = req("POST", f"/api/outbound/{out26}/confirm", {}, token=tok)
        check("出货单确认成功", s == 200, f"status={s} {d}")
        check("确认出货后未付款仍留在未结单 (双口径: 钱没付清)",
              _in_view(out26, "outbound", "open")
              and not _in_view(out26, "outbound", "closed"), f"status={s} {d}")
        s, d = req("POST", f"/api/orders/outbound/{out26}/payments",
                   {"amount": 200}, token=tok)
        check("出货单补记收款到付清", s == 200, f"status={s} {d}")
        check("付清后只在已结单/出货单",
              _in_view(out26, "outbound", "closed", "outbound")
              and not _in_view(out26, "outbound", "open"), f"status={s} {d}")

        # 26f. 自饮单下单即生效, 直接进已结单/出货单
        s, d = req("POST", "/api/outbound",
                   {"type": "personal", "warehouse_id": wh1,
                    "items": [{"wine_id": wid_o, "qty": 1}]}, token=tok)
        per26 = d.get("id")
        check("自饮单直接进已结单/出货单",
              s == 200 and _in_view(per26, "outbound", "closed", "outbound")
              and not _in_view(per26, "outbound", "open"), f"status={s} {d}")

        # 26g. 调拨单: 未确认在未结单, 确认后进已结单/调仓单
        s, d = req("POST", "/api/transfers",
                   {"wine_id": wid_o, "from_warehouse_id": wh1,
                    "to_warehouse_id": wh2, "qty": 1}, token=tok)
        s, ds = req("GET", "/api/transfers", token=tok)
        tr26 = [x for x in ds if x["transfer_no"] == d.get("transfer_no")][0]["id"]
        check("未确认调拨单在未结单、不在已结单",
              s == 200 and _in_view(tr26, "transfers", "open")
              and not _in_view(tr26, "transfers", "closed"), f"status={s} {d}")
        s, d = req("POST", f"/api/transfers/{tr26}/confirm", {}, token=tok)
        check("调拨单确认成功", s == 200, f"status={s} {d}")
        check("确认调拨后只在已结单/调仓单",
              _in_view(tr26, "transfers", "closed", "transfer")
              and not _in_view(tr26, "transfers", "open"), f"status={s} {d}")

        # 26i. 出货单部分确认后废剩余: 欠款保留, 再确认被拒
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_o, "qty": 2, "unit_price": 200}]},
                   token=tok)
        out26b = d.get("id")
        s, d = req("GET", f"/api/outbound/{out26b}", token=tok)
        item26o = d["items"][0]["id"]
        s, d = req("POST", f"/api/outbound/{out26b}/confirm",
                   {"items": [{"item_id": item26o, "qty": 1}]}, token=tok)
        check("出货单部分确认成功",
              s == 200 and d.get("status") == "partial", f"status={s} {d}")
        s, db1 = req("GET", "/api/dashboard", token=tok)
        cus_d1 = db1.get("customer_debt")
        s, d = req("POST", f"/api/orders/outbound/{out26b}/void", {}, token=tok)
        check("出货单部分确认后废剩余成功变已关闭",
              s == 200 and d.get("status") == "closed", f"status={s} {d}")
        s, db2 = req("GET", "/api/dashboard", token=tok)
        check("关闭后已确认部分的客户欠款保留",
              db2.get("customer_debt") == cus_d1,
              f"d1={cus_d1} d2={db2.get('customer_debt')}")
        s, d = req("POST", f"/api/outbound/{out26b}/confirm", {}, token=tok)
        check("已关闭的出货单再确认被拒 400", s == 400, f"status={s} {d}")
        check("废剩余后仍在未结单 (已确认部分有欠款)",
              _in_view(out26b, "outbound", "open")
              and not _in_view(out26b, "outbound", "closed"), f"status={s} {d}")
        s, d = req("POST", f"/api/orders/outbound/{out26b}/payments",
                   {"amount": 200}, token=tok)
        check("关闭的出货单补记收款到付清", s == 200, f"status={s} {d}")
        check("付清后进已结单/出货单",
              _in_view(out26b, "outbound", "closed", "outbound")
              and not _in_view(out26b, "outbound", "open"), f"status={s} {d}")

        # 26h. 前端: 未结单/已结单切换 + 四个类型按钮, 旧三个按钮已删
        s, html = req("GET", "/")
        check("单据页有未结单/已结单切换按钮",
              'id="ord-tab-open"' in html and 'id="ord-tab-closed"' in html
              and "未结单" in html and "已结单" in html, f"status={s}")
        check("已结单下有进货单/补货单/出货单/调仓单四个类型按钮",
              'id="ord-type-purchase"' in html and 'id="ord-type-opening"' in html
              and 'id="ord-type-outbound"' in html and 'id="ord-type-transfer"' in html
              and "补货单" in html and "调仓单" in html, f"status={s}")
        check("旧三个筛选按钮已删",
              "进货/补登单" not in html and "loadOrders('inbound')" not in html
              and "loadOrders('outbound')" not in html
              and "loadOrders('transfers')" not in html, f"status={s}")
        check("分类逻辑在场 (ordSettled/ordOwed/ordTypeOf/setOrdView/setOrdType)",
              "ordSettled" in html and "ordOwed" in html and "ordTypeOf" in html
              and "setOrdView" in html and "setOrdType" in html, f"status={s}")
        check("未结单也有类型筛选 (全部/进货/出货/调仓, 无补货)",
              'id="ord-type-row-open"' in html and 'id="ord-open-all"' in html
              and 'id="ord-open-purchase"' in html
              and 'id="ord-open-outbound"' in html
              and 'id="ord-open-transfer"' in html
              and 'id="ord-open-opening"' not in html, f"status={s}")
        check("未结单为空显示当前没有待处理单据",
              "当前没有待处理单据" in html, f"status={s}")
        check("已作废/已关闭/已完成徽标三色区分 (红 bad/黄 warn/绿)",
              ".badge.bad" in html and "badge${badgeCls}" in html
              and "badge bad" in html, f"status={s}")
        check("出货单卡片显示业务类型 (销售/自饮/赠送/破损)",
              "o.type==='sale'?'销售'" in html, f"status={s}")

        # 27. v2.10.0: 双口径分类、退货/纠错、防超卖、备份恢复
        s, d = req("POST", "/api/warehouses", {"name": "退货仓"}, token=tok)
        wh_ret = d.get("id")
        check("建退货仓", s == 200 and wh_ret, f"status={s} {d}")

        def _stock(wid, wine):
            _, _rows = req("GET", "/api/stock", token=tok)
            for _r in _rows:
                if _r["warehouse_id"] == wid and _r["wine_id"] == wine:
                    return _r["quantity"]
            return 0

        def _debt():
            _, _db = req("GET", "/api/dashboard", token=tok)
            return _db.get("customer_debt"), _db.get("supplier_debt")

        # 27a. 销售退货全流程
        s, w = req("POST", "/api/wines",
                   {"barcode": "6901234567898", "name": "退货测试酒",
                    "capacity_ml": 750, "sale_price": 200}, token=tok)
        wid_r = w.get("id")
        check("建退货测试酒", s == 200 and wid_r, f"status={s} {w}")
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid_r, "qty": 30, "unit_price": 100}]},
                   token=tok)
        check("退货测试酒补登 30 瓶", s == 200, f"status={s} {d}")
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_r, "qty": 10, "unit_price": 200}]},
                   token=tok)
        out_r = d.get("id")
        check("建销售单 10 瓶", s == 200 and out_r, f"status={s} {d}")
        s, d = req("POST", f"/api/outbound/{out_r}/confirm", {}, token=tok)
        check("销售单确认成功", s == 200, f"status={s} {d}")
        check("确认后主仓剩 20 瓶", _stock(wh1, wid_r) == 20,
              f"stock={_stock(wh1, wid_r)}")
        cd0, _ = _debt()
        s, d = req("GET", f"/api/outbound/{out_r}", token=tok)
        item_r = d["items"][0]["id"]
        s, d = req("POST", "/api/returns",
                   {"kind": "sale", "orig_order_id": out_r,
                    "warehouse_id": wh_ret, "reason": "客户不要了",
                    "items": [{"orig_item_id": item_r, "qty": 4,
                               "disposition": "normal"}]}, token=tok)
        ret_r = d.get("id")
        check("建销售退货单 4 瓶", s == 200 and ret_r, f"status={s} {d}")
        check("退货单在未结单/出货单",
              _in_view(ret_r, "returns", "open")
              and not _in_view(ret_r, "returns", "closed"), f"status={s} {d}")
        s, d = req("POST", "/api/returns",
                   {"kind": "sale", "orig_order_id": out_r,
                    "warehouse_id": wh_ret, "reason": "多退",
                    "items": [{"orig_item_id": item_r, "qty": 7,
                               "disposition": "normal"}]}, token=tok)
        check("超量退货被拒 400", s == 400 and "可退" in str(d),
              f"status={s} {d}")
        s, d = req("GET", f"/api/returns/{ret_r}", token=tok)
        ritem_r = d["items"][0]["id"]
        check("退货单明细关联原行、退货价冻结原行折后价",
              d["items"][0].get("orig_item_id") == item_r
              and d["items"][0].get("unit_price") == 200, f"status={s} {d}")
        s, d = req("POST", f"/api/returns/{ret_r}/confirm",
                   {"items": [{"item_id": ritem_r, "qty": 2}]}, token=tok)
        check("退货部分确认成功", s == 200 and d.get("status") == "partial",
              f"status={s} {d}")
        check("正常退货副仓 +2 瓶", _stock(wh_ret, wid_r) == 2,
              f"stock={_stock(wh_ret, wid_r)}")
        cd1, _ = _debt()
        check("退货确认后客户欠款减少 400", cd1 == cd0 - 400,
              f"d0={cd0} d1={cd1}")
        s, d = req("GET", f"/api/outbound/{out_r}", token=tok)
        check("原单行显示已退 2 瓶",
              d["items"][0].get("returned_qty") == 2, f"status={s} {d}")
        s, d = req("POST", f"/api/returns/{ret_r}/confirm", {}, token=tok)
        check("退货剩余全部确认成功",
              s == 200 and d.get("status") == "done", f"status={s} {d}")
        check("退货单确认完进已结单/出货单",
              _in_view(ret_r, "returns", "closed", "outbound")
              and not _in_view(ret_r, "returns", "open"), f"status={s} {d}")
        check("正常退货副仓共 +4 瓶", _stock(wh_ret, wid_r) == 4,
              f"stock={_stock(wh_ret, wid_r)}")
        s, d = req("POST", "/api/returns",
                   {"kind": "sale", "orig_order_id": out_r,
                    "warehouse_id": wh_ret, "reason": "退回来摔坏了",
                    "items": [{"orig_item_id": item_r, "qty": 2,
                               "disposition": "damage"}]}, token=tok)
        ret_r2 = d.get("id")
        check("建破损退货单 2 瓶", s == 200 and ret_r2, f"status={s} {d}")
        s, d = req("POST", f"/api/returns/{ret_r2}/confirm", {}, token=tok)
        check("破损退货确认成功", s == 200, f"status={s} {d}")
        check("破损退货不加库存 (副仓仍 4 瓶)",
              _stock(wh_ret, wid_r) == 4, f"stock={_stock(wh_ret, wid_r)}")
        cd2, _ = _debt()
        check("破损退货确认后客户欠款再减 800",
              abs(cd2 - (cd1 - 800)) < 0.01, f"d1={cd1} d2={cd2}")
        # 退货单废单释放占用: 可退 = 10 - 已退 6 = 4
        s, d = req("POST", "/api/returns",
                   {"kind": "sale", "orig_order_id": out_r,
                    "warehouse_id": wh_ret, "reason": "占位",
                    "items": [{"orig_item_id": item_r, "qty": 4,
                               "disposition": "normal"}]}, token=tok)
        ret_r3 = d.get("id")
        check("建退货单占满剩余可退量", s == 200 and ret_r3, f"status={s} {d}")
        s, d = req("POST", "/api/returns",
                   {"kind": "sale", "orig_order_id": out_r,
                    "warehouse_id": wh_ret, "reason": "超占",
                    "items": [{"orig_item_id": item_r, "qty": 1,
                               "disposition": "normal"}]}, token=tok)
        check("待确认退货单占用额度, 再退被拒 400", s == 400,
              f"status={s} {d}")
        s, d = req("POST", f"/api/orders/returns/{ret_r3}/void", {}, token=tok)
        check("未确认退货单整单废成功",
              s == 200 and d.get("status") == "voided", f"status={s} {d}")
        s, d = req("POST", "/api/returns",
                   {"kind": "sale", "orig_order_id": out_r,
                    "warehouse_id": wh_ret, "reason": "释放后重退",
                    "items": [{"orig_item_id": item_r, "qty": 2,
                               "disposition": "normal"}]}, token=tok)
        ret_r4 = d.get("id")
        check("废单释放占用后可重新退货", s == 200 and ret_r4, f"status={s} {d}")
        s, d = req("GET", f"/api/returns/{ret_r4}", token=tok)
        ritem_r4 = d["items"][0]["id"]
        s, d = req("POST", f"/api/returns/{ret_r4}/confirm",
                   {"items": [{"item_id": ritem_r4, "qty": 1}]}, token=tok)
        check("退货单部分确认成功", s == 200, f"status={s} {d}")
        s, d = req("POST", f"/api/orders/returns/{ret_r4}/void", {}, token=tok)
        check("退货单部分确认后废剩余变已关闭",
              s == 200 and d.get("status") == "closed", f"status={s} {d}")
        s, d = req("POST", "/api/returns",
                   {"kind": "sale", "orig_order_id": out_r,
                    "warehouse_id": wh_ret, "reason": "超量",
                    "items": [{"orig_item_id": item_r, "qty": 4,
                               "disposition": "normal"}]}, token=tok)
        check("已关闭退货单只按已确认计已退, 余量 3 瓶退 4 被拒",
              s == 400, f"status={s} {d}")
        s, d = req("POST", "/api/returns",
                   {"kind": "sale", "orig_order_id": out_r,
                    "warehouse_id": wh_ret, "reason": "退完",
                    "items": [{"orig_item_id": item_r, "qty": 3,
                               "disposition": "normal"}]}, token=tok)
        ret_r5 = d.get("id")
        check("建最后 3 瓶退货单", s == 200 and ret_r5, f"status={s} {d}")
        s, d = req("POST", f"/api/returns/{ret_r5}/confirm", {}, token=tok)
        check("最后退货确认成功", s == 200, f"status={s} {d}")
        check("退货确认完的单不能再废", req(
            "POST", f"/api/orders/returns/{ret_r5}/void", {}, token=tok)[0] == 400,
            f"status={s} {d}")

        # 27b. 采购退货全流程
        s, w = req("POST", "/api/wines",
                   {"barcode": "6901234567899", "name": "采购退货酒",
                    "capacity_ml": 500, "sale_price": 100}, token=tok)
        wid_p = w.get("id")
        check("建采购退货酒", s == 200 and wid_p, f"status={s} {w}")
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid_p, "qty": 10, "unit_price": 50}]},
                   token=tok)
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_p, "qty": 5, "unit_price": 60}]},
                   token=tok)
        in_p = d.get("id")
        check("建进货单 5 瓶", s == 200 and in_p, f"status={s} {d}")
        s, d = req("POST", f"/api/inbound/{in_p}/confirm", {}, token=tok)
        check("进货单确认成功", s == 200, f"status={s} {d}")
        check("进货确认后主仓 15 瓶", _stock(wh1, wid_p) == 15,
              f"stock={_stock(wh1, wid_p)}")
        _, sd0 = _debt()
        s, d = req("GET", f"/api/inbound/{in_p}", token=tok)
        item_p = d["items"][0]["id"]
        s, d = req("POST", "/api/returns",
                   {"kind": "purchase", "orig_order_id": in_p,
                    "reason": "质量问题",
                    "items": [{"orig_item_id": item_p, "qty": 3}]}, token=tok)
        ret_p = d.get("id")
        check("建采购退货单 3 瓶", s == 200 and ret_p, f"status={s} {d}")
        check("采购退货单在未结单/进货单",
              _in_view(ret_p, "returns", "open")
              and not _in_view(ret_p, "returns", "closed"), f"status={s} {d}")
        s, d = req("POST", f"/api/returns/{ret_p}/confirm", {}, token=tok)
        check("采购退货确认成功", s == 200, f"status={s} {d}")
        check("采购退货确认后主仓 12 瓶", _stock(wh1, wid_p) == 12,
              f"stock={_stock(wh1, wid_p)}")
        _, sd1 = _debt()
        check("采购退货确认后供应商欠款减少 180", sd1 == sd0 - 180,
              f"d0={sd0} d1={sd1}")
        s, d = req("POST", "/api/returns",
                   {"kind": "purchase", "orig_order_id": in_p,
                    "reason": "超量",
                    "items": [{"orig_item_id": item_p, "qty": 3}]}, token=tok)
        check("采购超量退货被拒 400", s == 400, f"status={s} {d}")
        # 采购退货确认时库存不足被拒: 先把 12 瓶全卖掉
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_p, "qty": 12, "unit_price": 100}]},
                   token=tok)
        out_p = d.get("id")
        s, d = req("POST", f"/api/outbound/{out_p}/confirm", {}, token=tok)
        check("卖掉 12 瓶后主仓 0 瓶", _stock(wh1, wid_p) == 0,
              f"stock={_stock(wh1, wid_p)}")
        s, d = req("POST", "/api/returns",
                   {"kind": "purchase", "orig_order_id": in_p,
                    "reason": "再退",
                    "items": [{"orig_item_id": item_p, "qty": 2}]}, token=tok)
        ret_p2 = d.get("id")
        check("建采购退货单 2 瓶", s == 200 and ret_p2, f"status={s} {d}")
        s, d = req("POST", f"/api/returns/{ret_p2}/confirm", {}, token=tok)
        check("采购退货确认时库存不足被拒 400 且报错含缺货信息",
              s == 400 and "缺" in str(d) and "主仓" in str(d),
              f"status={s} {d}")

        # 27c. 付款撤销
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_r, "qty": 2, "unit_price": 200}]},
                   token=tok)
        out_c = d.get("id")
        s, d = req("POST", f"/api/outbound/{out_c}/confirm", {}, token=tok)
        check("撤销测试销售单确认成功", s == 200, f"status={s} {d}")
        cd0, _ = _debt()
        s, d = req("POST", f"/api/orders/outbound/{out_c}/payments",
                   {"amount": 150}, token=tok)
        check("补记收款 150", s == 200, f"status={s} {d}")
        cd1, _ = _debt()
        check("收款后客户欠款减少 150", abs(cd1 - (cd0 - 150)) < 0.01,
              f"d0={cd0} d1={cd1}")
        s, d = req("GET", f"/api/outbound/{out_c}", token=tok)
        pay_c = [p for p in d.get("payments") or [] if not p.get("voided")]
        check("单据详情带付款记录", len(pay_c) == 1, f"status={s} {d}")
        pid_c = pay_c[0]["id"]
        s, d = req("POST",
                   f"/api/orders/outbound/{out_c}/payments/{pid_c}/void",
                   {"reason": "记错了"}, token=tok)
        check("撤销收款成功", s == 200, f"status={s} {d}")
        cd2, _ = _debt()
        check("撤销后客户欠款恢复", cd2 == cd0, f"d0={cd0} d2={cd2}")
        s, d = req("POST",
                   f"/api/orders/outbound/{out_c}/payments/{pid_c}/void",
                   {"reason": "再撤"}, token=tok)
        check("重复撤销被拒 400", s == 400, f"status={s} {d}")
        s, d = req("POST", f"/api/orders/outbound/{out_c}/payments",
                   {"amount": 50}, token=tok)
        s, d = req("GET", f"/api/outbound/{out_c}", token=tok)
        pid_c2 = [p for p in d.get("payments") or [] if not p.get("voided")][0]["id"]
        s, d = req("POST",
                   f"/api/orders/outbound/{out_c}/payments/{pid_c2}/void",
                   {}, token=tok)
        check("撤销理由必填 (无理由被拒 400)", s == 400, f"status={s} {d}")
        s, d = req("POST",
                   f"/api/orders/outbound/{out_c}/payments/{pid_c2}/void",
                   {"reason": "多记了"}, token=tok)
        check("有理由撤销成功", s == 200, f"status={s} {d}")
        s, logs = req("GET", "/api/audit-logs", token=tok)
        acts = {l["action"] for l in logs} if isinstance(logs, list) else set()
        check("审计记录有撤销收款",
              "撤销收款" in acts, f"actions={sorted(acts)}")
        # 进货付款撤销
        s, d = req("POST", "/api/inbound",
                   {"type": "purchase", "supplier_id": sup, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_r, "qty": 1, "unit_price": 100}]},
                   token=tok)
        in_c = d.get("id")
        s, d = req("POST", f"/api/inbound/{in_c}/confirm", {}, token=tok)
        s, d = req("POST", f"/api/orders/inbound/{in_c}/payments",
                   {"amount": 100}, token=tok)
        s, d = req("GET", f"/api/inbound/{in_c}", token=tok)
        pid_in = [p for p in d.get("payments") or [] if not p.get("voided")][0]["id"]
        _, sd0 = _debt()
        s, d = req("POST",
                   f"/api/orders/inbound/{in_c}/payments/{pid_in}/void",
                   {"reason": "付错了"}, token=tok)
        check("撤销进货付款成功", s == 200, f"status={s} {d}")
        _, sd1 = _debt()
        check("撤销后供应商欠款恢复 (+100)", sd1 == sd0 + 100,
              f"d0={sd0} d1={sd1}")

        # 27d. 防超卖
        s, w = req("POST", "/api/wines",
                   {"barcode": "6901234567900", "name": "防超卖酒",
                    "capacity_ml": 750, "sale_price": 200}, token=tok)
        wid_s = w.get("id")
        check("建防超卖酒", s == 200 and wid_s, f"status={s} {w}")
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid_s, "qty": 5, "unit_price": 100}]},
                   token=tok)
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_s, "qty": 5, "unit_price": 200}]},
                   token=tok)
        out_s1 = d.get("id")
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_s, "qty": 5, "unit_price": 200}]},
                   token=tok)
        out_s2 = d.get("id")
        check("两张 5 瓶出货单建单都成功 (下单不扣库存)",
              s == 200 and out_s1 and out_s2, f"status={s} {d}")
        s, d = req("POST", f"/api/outbound/{out_s1}/confirm", {}, token=tok)
        check("第一张确认成功", s == 200, f"status={s} {d}")
        check("确认后主仓 0 瓶", _stock(wh1, wid_s) == 0,
              f"stock={_stock(wh1, wid_s)}")
        s, d = req("POST", f"/api/outbound/{out_s2}/confirm", {}, token=tok)
        check("第二张确认被拒 400 且报错含仓库/酒名/缺多少瓶",
              s == 400 and "缺" in str(d) and "主仓" in str(d)
              and "防超卖酒" in str(d), f"status={s} {d}")
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid_s, "qty": 2, "unit_price": 100}]},
                   token=tok)
        s, d = req("POST", f"/api/outbound/{out_s2}/confirm",
                   {"items": []}, token=tok)
        check("空确认请求被拒 400", s == 400, f"status={s} {d}")
        s, dd = req("GET", f"/api/outbound/{out_s2}", token=tok)
        item_s2 = dd["items"][0]["id"]
        s, d = req("POST", f"/api/outbound/{out_s2}/confirm",
                   {"items": [{"item_id": item_s2, "qty": 2}]}, token=tok)
        check("部分确认到可用量内成功 (2 瓶)",
              s == 200 and d.get("status") == "partial", f"status={s} {d}")
        check("部分确认后主仓 0 瓶", _stock(wh1, wid_s) == 0,
              f"stock={_stock(wh1, wid_s)}")
        # 建单时同酒多行汇总校验
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid_s, "qty": 3, "unit_price": 100}]},
                   token=tok)
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_s, "qty": 2, "unit_price": 200},
                              {"wine_id": wid_s, "qty": 2, "unit_price": 200}]},
                   token=tok)
        check("同酒多行合计超库存建单被拒 400 且报错汇总",
              s == 400 and "缺" in str(d), f"status={s} {d}")
        # 调拨确认超卖
        s, d = req("POST", "/api/transfers",
                   {"wine_id": wid_s, "from_warehouse_id": wh1,
                    "to_warehouse_id": wh_ret, "qty": 3}, token=tok)
        s, ds = req("GET", "/api/transfers", token=tok)
        tr_s = [x for x in ds if x["transfer_no"] == d.get("transfer_no")][0]["id"]
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid_s, "qty": 3, "unit_price": 200}]},
                   token=tok)
        out_s3 = d.get("id")
        s, d = req("POST", f"/api/outbound/{out_s3}/confirm", {}, token=tok)
        check("卖掉 3 瓶后主仓 0 瓶", _stock(wh1, wid_s) == 0,
              f"stock={_stock(wh1, wid_s)}")
        s, d = req("POST", f"/api/transfers/{tr_s}/confirm", {}, token=tok)
        check("调拨确认时库存不足被拒 400 且报错含缺货信息",
              s == 400 and "缺" in str(d) and "主仓" in str(d),
              f"status={s} {d}")
        # 立即生效的自饮建单时也要校验库存
        s, d = req("POST", "/api/outbound",
                   {"type": "personal", "warehouse_id": wh1,
                    "items": [{"wine_id": wid_s, "qty": 1}]}, token=tok)
        check("自饮建单时库存不足被拒 400", s == 400 and "缺" in str(d),
              f"status={s} {d}")

        # 27e. 备份与恢复 (对正在跑的测试库做在线备份再恢复, 行数对照)
        import sqlite3 as _sqlite3
        bdir = os.path.join(tmp, "backups")
        env_b = dict(os.environ, VIN_ROUGE_DB=os.path.join(tmp, "test.db"),
                     VIN_ROUGE_BACKUP_DIR=bdir, PATH=os.environ["PATH"])
        r = subprocess.run(["/bin/sh", str(BASE / "scripts" / "backup.sh")],
                           capture_output=True, text=True, env=env_b)
        check("backup.sh 跑成功", r.returncode == 0,
              f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
        daily = sorted(Path(bdir, "daily").glob("vin_rouge-*.db")) \
            if Path(bdir, "daily").exists() else []
        check("备份文件已生成", len(daily) == 1, f"files={daily}")
        bak = str(daily[0]) if daily else ""
        rdir = os.path.join(tmp, "restored")
        r = subprocess.run(["/bin/sh", str(BASE / "scripts" / "restore.sh"),
                            bak, rdir], capture_output=True, text=True)
        check("restore.sh 跑成功", r.returncode == 0 and "恢复完成" in r.stdout,
              f"rc={r.returncode} out={r.stdout.strip()} err={r.stderr.strip()}")
        tables = ["users", "wines", "stock", "inbound_orders",
                  "outbound_orders", "return_orders", "payments"]
        _okc = True
        for _t in tables:
            _a = _sqlite3.connect(os.path.join(tmp, "test.db")).execute(
                f"SELECT COUNT(*) FROM {_t}").fetchone()[0]
            _b = _sqlite3.connect(os.path.join(rdir, "vin_rouge.db")).execute(
                f"SELECT COUNT(*) FROM {_t}").fetchone()[0]
            if _a != _b:
                _okc = False
        check("恢复后的库行数与原库一致", _okc, f"tables={tables}")

        # 27f. 前端: 退货页/退货卡片/付款撤销/多付显示在场
        s, html = req("GET", "/")
        check("退货单页面与表单在场",
              'id="page-returns"' in html and 'id="ret-reason"' in html
              and 'id="ret-wh"' in html and 'id="ret-lines"' in html,
              f"status={s}")
        check("退货卡片与入口在场 (returnCard/openReturn/退货按钮)",
              "returnCard" in html and "openReturn" in html
              and "退货" in html, f"status={s}")
        check("付款撤销入口在场 (voidPayment)",
              "voidPayment" in html and "撤销" in html, f"status={s}")
        check("欠款为负显示多付", "多付 ¥" in html, f"status={s}")
        check("原单行显示已退数量", "已退" in html, f"status={s}")
        check("前端欠款口径含退货冲减 (returned_amount/ordOwed)",
              "ordOwed" in html and "returned_amount" in html, f"status={s}")

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
