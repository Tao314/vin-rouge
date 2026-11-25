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

        # 1b. 版本接口公开返回 v2.2.0（期望值写死在测试里，运行时版本以 app.py 的 APP_VERSION 为准）
        s, d = req("GET", "/api/version")
        check("版本接口 /api/version 返回 v2.2.0", s == 200 and d.get("version") == "v2.2.0", f"status={s} {d}")
        s, d = req("GET", "/api/config")
        check("配置接口 /api/config 版本一致", s == 200 and d.get("version") == "v2.2.0" and "build_date" in d, f"status={s} {d}")

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
                   {"username": "admin2", "display_name": "二管",
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

        # 7. 补登 10 瓶 (成本 100)
        s, d = req("POST", "/api/inbound",
                   {"type": "opening", "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 10, "unit_price": 100}]},
                   token=tok)
        check("补登 10 瓶", s == 200, f"status={s} {d}")
        opening_id = d.get("id")

        # 8. 进货 5 瓶 (进价 120, 运费 30, 货款应为 600 不含运费)
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

        # 9. 出货 3 瓶 (卖价 200), 库存剩 12
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1, "freight": 10,
                    "items": [{"wine_id": wid, "qty": 3, "unit_price": 200}]},
                   token=tok)
        out_id = d.get("id")
        check("出货 3 瓶", s == 200, f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("出货后库存剩 12", stock == 12, f"stock={stock}")

        # 10. 出货 99 瓶被拦住
        s, d = req("POST", "/api/outbound",
                   {"customer_id": cus, "warehouse_id": wh1,
                    "items": [{"wine_id": wid, "qty": 99, "unit_price": 200}]},
                   token=tok)
        check("出货 99 瓶被拦住", s == 400, f"status={s} {d}")
        s, ws = req("GET", "/api/wines", token=tok)
        stock = [w["stock_total"] for w in ws if w["id"] == wid][0]
        check("被拦后库存仍为 12", stock == 12, f"stock={stock}")

        # 11. 调拨 2 瓶 主仓->副仓
        s, d = req("POST", "/api/transfers",
                   {"wine_id": wid, "from_warehouse_id": wh1,
                    "to_warehouse_id": wh2, "qty": 2}, token=tok)
        check("调拨 2 瓶", s == 200, f"status={s} {d}")
        s, st = req("GET", "/api/stock", token=tok)
        m = {(x["warehouse_id"], x["wine_id"]): x["quantity"] for x in st}
        check("调拨后主仓 10 / 副仓 2",
              m.get((wh1, wid)) == 10 and m.get((wh2, wid)) == 2, str(m))

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

        # 14. audit_logs 有记录
        s, logs = req("GET", "/api/audit-logs", token=tok)
        acts = {l["action"] for l in logs} if isinstance(logs, list) else set()
        need = {"登录", "进货", "出货", "补登期初", "调拨", "收款", "新增管理员",
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
