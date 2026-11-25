# vin-rouge v2 重建 SUMMARY

- 分支: `rebuild/v2-20261125`（基于 main e0a74c1，main 未动，未 push）
- 云电脑本地路径: `~/vin-rouge`（/data/home/vin-rouge）
- 旧版文件已清空重建（保留 .git），旧版备份在 GitHub 分支 `backup/v0.4.0-20261125`，未动。

## 改了哪些文件（wc -l）

| 文件 | 行数 | 说明 |
|---|---|---|
| app.py | 907 | FastAPI + SQLite 单文件后端，全部 API |
| static/index.html | 175 | 单文件前端，内联 CSS/JS，无 CDN，手机优先 |
| README.md | 49 | 部署、首次密码、条码接口说明 |
| requirements.txt | 3 | fastapi、uvicorn、pillow |
| install.sh | 30 | 部署到 /opt/vin-rouge，systemd 服务 vin-rouge |
| backup.sh | 9 | 备份 vin_rouge.db + uploads/ |
| .gitignore | 5 | 含 vin_rouge.db、uploads/、.venv/、__pycache__/ |
| tests/smoke_test.py | 216 | 真实流程冒烟测试（起真实 uvicorn 服务断言） |
| SUMMARY.md | — | 本文件 |

## 验证结果（云电脑本地，全部通过）

- `python3 -m py_compile app.py tests/smoke_test.py`：PASS
- `pip install -r requirements.txt`（venv）：PASS
- uvicorn 启动后首页 200：PASS；未登录调业务接口 401：PASS
- `tests/smoke_test.py`（`./.venv/bin/python tests/smoke_test.py`，26 项，PASS 26 / FAIL 0）：
  - 未登录调业务接口返回 401：PASS
  - tristan 默认密码登录成功：PASS；must_change_password 为真：PASS
  - 未改默认密码时业务接口被拦 403：PASS
  - 改密后能进业务页：PASS
  - 新建第二个管理员：PASS；用它登录成功：PASS；能看首页（含毛利，即能看到进价和利润，不分角色）：PASS
  - 停用该账号后它再登录失败（401）：PASS
  - 建酒：PASS；补登 10 瓶（单号 BD20261125-001）：PASS
  - 进货 5 瓶（单号 RK20261125-001）：PASS；运费与货款分开（goods_total=600，不含运费 30）：PASS
  - 出货 3 瓶（单号 CK20261125-001）：PASS；出货后库存剩 12：PASS
  - 出货 99 瓶被拦住（400，提示库存不足）：PASS；被拦后库存仍为 12：PASS
  - 调拨 2 瓶（单号 DB20261125-001）：PASS；调拨后主仓 10 / 副仓 2：PASS
  - 补记收款后状态变已付：PASS
  - 出货分享图为有效 PNG（宽 1080）：PASS；进货分享图为有效 PNG（宽 1080）：PASS
  - audit_logs 有关键操作记录（登录、进货、出货、补登期初、调拨、收款、新增管理员等 10 种）：PASS
- 额外验证（冒烟测试之外，手动 curl）：
  - 分享图中文渲染：PASS（云电脑有 Noto Sans CJK 字体，图中中文正常，非 tofu；非白像素 11458）
  - 图片上传（原始 body 方式）：PASS，文件落到 uploads/，GET /uploads/... 200
  - 条码接口无 token 返回 401：PASS

## 风险点

- 图片上传没用 `python-multipart`：FastAPI 的 `UploadFile` 需要该包，为把依赖严格控制在 fastapi/uvicorn/pillow 三个，上传改走原始 body + `X-Filename` 头，前端已同步改好；如后续改回标准 multipart 表单，需在 requirements.txt 加 `python-multipart`。
- 密码哈希用标准库 `hashlib.pbkdf2_hmac`（20 万轮），没用 bcrypt/passlib，原因同上（不加依赖）；强度够用，如要换 bcrypt 需加依赖。
- 条码网上接口未实测：`BARCODE_API_URL` 为空时返回 `{found:false}` 走手填；只做了通用 JSON 字段兼容，未对接任何真实供应商接口，真实接口的字段名可能需要再适配。进口条码覆盖低、新品可能查不到（已写进 README）。
- 分享图字体依赖云电脑/部署机系统字体：本机用 Noto Sans CJK 渲染中文；部署到 VPS 后若无 CJK 字体会回落到 DejaVu（中文显示为方框），需在 VPS 上装 `fonts-noto-cjk` 或把字体文件放 `static/fonts/`。
- 利润为估算值：卖价减该酒最近进货成本价（或补登成本价），不做逐瓶批次成本核算。
- 单据目前每单只支持一行商品（前端表单单行；后端 API 支持多行 items 数组）。
- install.sh 未在真实 VPS 上跑过，只在云电脑上验证了它引用的文件齐全；`sudo` 与 systemd 部分需 Tristan 在 VPS 上实测。

## 还缺什么

- 未 push、未合 main：等 Tristan 验收批复后再推分支、开 PR、合 main。
- 未在 VPS 上部署验证（systemd 服务、反向代理、域名均未做）。
- 条码接口未接真实供应商，需 Tristan 确定用哪家接口再填 `BARCODE_API_URL` / `BARCODE_API_KEY`。
- 前端单据多行商品、按日期/对方筛选单据列表（后端 API 已支持筛选参数，前端列表页暂未做筛选控件）。
- 运行产物已清理：验证用的 `vin_rouge.db`、测试上传图片已删除，`uploads/` 为空；首次启动会自动建库和种子数据。


## 版本追踪（v2.0.0，2026-11-25 补）

- 唯一版本源：`app.py` 的 `APP_VERSION = "v2.0.0"`，`BUILD_DATE = "2026-11-25"`。
- 同步点：`GET /api/version`、`GET /api/config`（都返回 version、build_date、git commit；config 还返回 port）、FastAPI `version=APP_VERSION`、前端页脚和登录页（JS 调 `/api/version` 动态取，不写死）、README 顶部和 [版本演变]。
- 升版规则：只改 `APP_VERSION`，再在 README [版本演变] 加一条记录；冒烟测试含版本接口断言（期望值写死在测试里作核对）。
- 本次改动：app.py 1069 → 1097 行、static/index.html 185 → 189 行、tests/smoke_test.py 308 → 314 行、README.md 51 → 99 行（全文重写核对）。
- README 核对结果：功能清单与实现一致（多管理员全权限、自饮/赠送/破损、折扣、分享图、条码接口）；部署步骤、首次密码、端口 9000、数据库 `vin_rouge.db`、备份、条码配置、fonts-noto-cjk、卸载（服务名 `vin-rouge`）均已按新版写准；旧版 `wine-system` / `data/wine.db` 的过时说明已清除，只在版本演变和卸载注意里提一行作区分。
