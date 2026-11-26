# 商品价格监控采集平台

面向多区域站点的商品价格抓取、规格归一、比价计算与变价告警的采集治理平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8020/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5200/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8020`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集站点目录 | `shop_site` | 站点配置 | 站点名称、站点域名、采集区域 |
| 价格爬取任务 | `price_crawler` | 爬取任务 | 任务名称、目标站点、商品关键词 |
| 页面解析模板 | `page_parser` | 解析模板 | 模板名称、适用站点、选择器路径 |
| 规格归一器 | `spec_normalizer` | 规格归一规则 | 规则名称、源规格文本、归一规格 |
| 货币换算器 | `currency_conv` | 汇率快照 | 快照日期、源货币、目标货币 |
| 价格基线库 | `price_baseline` | 价格基线 | 商品标识、归一规格、基线价格 |
| 比价计算器 | `price_compare` | 比价结果 | 比价批次、商品标识、最低价站点 |
| 变价告警规则 | `price_alert` | 告警规则 | 规则名称、监控商品、价差阈值 |
| 商品登记中心 | `goods_registry` | 商品档案 | 商品标识、商品名称、品牌 |
| 规格匹配器 | `sku_match` | 匹配记录 | 匹配批次、商品档案、候选规格 |
| 页面样本库 | `sample_store` | 页面样本 | 样本批次、站点名称、原始HTML摘要 |
| 解析质量审计 | `parse_audit` | 审计明细 | 审计时间、解析模板、样本总数 |
| 采集调度计划 | `crawl_schedule` | 调度计划 | 计划名称、关联任务、调度周期 |
| 抓取频控 | `rate_limit` | 频控规则 | 规则名称、目标站点、每分钟上限 |
| 代理线路池 | `proxy_route` | 代理线路 | 线路标识、线路区域、可用性评分 |
| 失败隔离区 | `failure_quarantine` | 隔离记录 | 样本指纹、失败阶段、失败原因 |
| 比价导出通道 | `price_export` | 导出任务 | 任务名称、商品范围、导出格式 |
| 变价历史流水 | `change_history` | 变价记录 | 商品标识、旧价格、新价格 |
| 人工复核队列 | `match_review` | 复核记录 | 记录编号、匹配记录、复核结论 |
| 采集量指标 | `collect_metrics` | 指标记录 | 统计时间、站点名称、抓取量 |

## SQLite 迁移与新旧入口续办

价格基线库与未完成比价已增加 SQLite 持久化、版本绑定和交接回执；其他模块仍使用原内存示例仓库。
默认数据库文件为 `backend/data/price_watch.sqlite3`，可用 `PRICE_WATCH_SQLITE_PATH`
（或 `PRICE_WATCH_DB` / `DATABASE_PATH` / `DATABASE_URL=sqlite:...`）覆盖。

常用接口：

```bash
# 构建/部署期原示例数据自检
cd backend && .venv/bin/python -m app.verify

# 幂等迁移并切换到 SQLite 新入口；重复调用不会复制受理
curl -X POST http://127.0.0.1:8020/api/migration/start -H 'Content-Type: application/json' -d '{}'

# 只原子迁移核对，不切换
curl -X POST http://127.0.0.1:8020/api/migration/prepare -H 'Content-Type: application/json' -d '{}'

# 迁移行数、价格、币种、来源清单
curl http://127.0.0.1:8020/api/migration/inventory

# 同一批次的旧/新入口实际回执
curl http://127.0.0.1:8020/api/legacy/continuation/K
curl http://127.0.0.1:8020/api/new/continuation/K

# 旧入口继续、新入口领取/继续
curl -X POST http://127.0.0.1:8020/api/legacy/continuation/K/continue \
  -H 'Content-Type: application/json' \
  -d '{"baseline_version_key":"L8@v1","site_prices":[{"site":"A站","price":35}]}'

# 给未办任务登记计价依据、按绑定版本复算基线、追溯原记录
curl -X POST http://127.0.0.1:8020/api/price_compare/8001/basis -H 'Content-Type: application/json' -d '{...}'
curl -X POST http://127.0.0.1:8020/api/price_compare/8001/recompute-baseline -d '{}'
curl http://127.0.0.1:8020/api/price_compare/8001/trace

# 基线更正会追加新版本（例如 L8@v1=37 -> L8@v2=41），旧结果仍绑定原版本
curl -X POST http://127.0.0.1:8020/api/price_baseline/L8/corrections \
  -H 'Content-Type: application/json' -d '{"new_price":41,"reason":"核价更正"}'

# 切换故障演练与回滚；SQLite 数据和唯一回执保留
curl -X POST http://127.0.0.1:8020/api/migration/simulate-failure -d '{}'
curl -X POST http://127.0.0.1:8020/api/migration/rollback -H 'Content-Type: application/json' -d '{"reason":"回滚"}'

# 一键构造“两个规格 + 一份未完成 K + L8 37→41”交接演练
curl -X POST http://127.0.0.1:8020/api/migration/scenarios/k-l8 \
  -H 'Content-Type: application/json' \
  -d '{"continue_during_handoff":true,"site_prices":[{"site":"A站","price":35}]}'

# 命令行/CI 完整不变量自检
cd backend && .venv/bin/python -I scripts/verify_handoff.py
```

续办回执中的 `receipt_type` / `kind` 为：

- `confirmed_result`：已有确认结果，只能查看/追溯/复算，任何入口不能再领取成第二份有效结果；
- `actionable_todo`：仍是可操作待办，可凭同一 `receipt_no` 继续或登记计价依据。

存量基线没有币种时不会伪造币种，统一写为 `待核对` / `currency_status=missing_pending_check`。
旧的已计算结果如果没有绑定价格版本、规格、来源或样本依据，复算接口返回
`missing_calculation_basis`、具体 `missing` 缺项和 `original_record` 原记录，补齐前不会按当前价擅自复算。

前端“价格基线库”和“比价计算器”页面顶部均有迁移/交接面板，可直接启动迁移、模拟故障回滚、
构造 K/L8 演练，并同时读取旧入口与新入口的真实 HTTP 回执。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
