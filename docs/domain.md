# 收入归属后端 · 领域目录

本文档与 `contracts/domain.schema.json` 配套。场景：一家文化平台把**非遗直播、按集短剧、连载网文**放进同一会员体系；主播打赏、短剧解锁、网文订阅、广告分成、联合会员退款在不同结算周期到达。后端的职责是回答三个验收问题：

1. **创作者视角**：从一笔收入出发，能看见它对应的消费、作品版本与扣减原因。
2. **财务视角**：能重放整批分录，验证借贷守恒、试算平衡、封账后不被改写。
3. **趋势视角**：只能读取公开聚合，读不到个人阅读轨迹，也读不到未公开单价。

## 1. 标识与不可变约定

- 事件一经接收，`event_id` / `occurred_at` / `version` 不得原地改写；更正一律追加后继事件，用 `links: supersedes` 指向被更正事件。
- 后继事件继续使用既有契约标识：`aggregate_type` / `aggregate_id` 不变，`version` 单调递增。
- 历史五事件（`RIGHT_REGISTERED`、`CONSUMPTION_RECORDED`、`REVENUE_ALLOCATED`、`DISPUTE_OPENED`、`SETTLEMENT_CLOSED`）与四聚合（`content_asset`、`rights_term`、`revenue_event`、`settlement_batch`）保持有效，新事件是其上的细化。
- 金额一律带币种；分录金额用字符串定点数，避免浮点。

## 2. 聚合

| aggregate_type | 含义 | 关键标识 |
| --- | --- | --- |
| `work` | 原始作品（网文、剧目、非遗节目） | `work_id` |
| `content_version` | 章节 / 直播场次 / 短剧单集等可消费版本 | `version_id`，归属某 `work` |
| `adaptation_link` | 跨媒介改编关系 | 源作品版本 → 派生版本，含授权范围 |
| `contribution` | 贡献者在某作品/版本上的贡献角色 | 作者、主播、表演者、剪辑师… |
| `rights_grant` | 地域与期限权利（可修订/冻结/解除/退出） | 授权范围 `scope` |
| `price_plan` | 价格方案（会员、按集解锁、打赏、广告） | `confidential=true` 即未公开单价 |
| `payment` | 用户支付（充值、联合会员、单购） | 支付号，不向趋势投影开放 |
| `entitlement` | 支付换取的权益（会员期 / 币 / 解锁） | 链接 payment |
| `consumption` | 一次消费（阅读章节、观看场次、解锁剧集、打赏） | 链接 version 与 entitlement |
| `material_reuse` | 回放/剪辑对已有素材的复用登记 | 链接 reuse_of 版本 |
| `channel_statement` | 渠道单（广告分成等外部周期结算单） | 可迟到 |
| `tax_line` | 税费（代扣、流转税） | |
| `revenue_event` | 一笔归属后的收入及其分账、扣减 | 财务核心 |
| `settlement_batch` | 结算批次（周期、封账状态） | |
| `content_asset` / `rights_term` | 历史兼容聚合 | 仅旧记录使用 |

## 3. 事件目录

负载写在 `body`，归属主体写在 `subjects`，复式分录写在 `effects`，关联写在 `links`。

### 3.1 作品与版本

- **WORK_REGISTERED** `work`：登记原始作品。`body`: `{title, medium(novel|short_drama|live_heritage|…), ip_owner}`。
- **VERSION_REGISTERED** `content_version`：登记章节/场次/单集版本。`body`: `{version_no, kind(chapter|live_session|drama_episode|replay|clip), published_at}`；`links: of_work`。
- **ADAPTATION_LINKED** `adaptation_link`：登记改编关系。`links: source_work, source_version(可选), derived_version`；`body`: `{adaptation_type, license_scope}`。一次跨媒介改编「按哪份合同分账」由本关系上挂的 `rights_grant` 决定，而不是按收入到达渠道猜测。

### 3.2 贡献者与权利

- **CONTRIBUTOR_ADDED** `contribution`：`body`: `{contributor_id, role, from_version?}`；`links: work/version`。
- **RIGHTS_GRANTED** `rights_grant`：授权。`body`: `{grantee, territories:[…], exclusive, valid_from, valid_to, media:[…], uses:[…], contract_ref}`。`scope` 是判定一切约束的唯一依据。
- **GRANT_AMENDED** `rights_grant`：合同修订。只写 `body.changes` 中显式列出的范围；未列出的地域/期限/媒介不受影响（*scoped amendment*）。`links: amends`。
- **GRANT_FROZEN** `rights_grant`：侵权冻结。只冻结 `body.scope` 命中的权利，相应分账进入暂扣而非核销。
- **GRANT_RELEASED** `rights_grant`：冻结解除，暂扣款按 3.6 释放。
- **GRANT_WITHDRAWN** `rights_grant`：作者退出。仅对 `valid_from` 之后且在退出范围内的未来收入生效；历史已确认收入不追溯。
- **CONTENT_TAKEN_DOWN** `content_version`：内容下架。`body.scope` 限定下架的版本/地域；阻止该范围内**新增**消费确认，不自动推翻已封账收入。

### 3.3 价格、支付与权益

- **PRICE_PLAN_FILED** `price_plan`：`body`: `{plan_type(membership|unlock|tip|ad_share|bundle), price?, currency, unit, confidential}`。未公开单价 `confidential=true`，`visibility=finance_internal`。
- **USER_PAYMENT_RECEIVED** `payment`：`body`: `{amount, channel, bundle_id?}`；典型分录：借 `cash_clearing`，贷 `deferred_revenue`（会员/预付）或贷 `revenue:*`（即时消费）。**不含个人身份字段进入下游。**
- **ENTITLEMENT_ISSUED** `entitlement`：`links: payment`；`body`: `{kind(membership|coins|unlock), period_start, period_end}`。

### 3.4 消费与复用

- **CONSUMPTION_RECORDED** `consumption`：`links: version, entitlement, price_plan`；`body`: `{consumed_at, quantity, evidence_ref}`。历史事件同名，语义保持。
  - 会员消费触发**递延收入分期确认**（见 3.5）。
  - 打赏/单集解锁可即时确认。
- **MATERIAL_REUSE_RECORDED** `material_reuse`：直播回放、短剧剪辑复用已计酬素材。`links: derived_version, reuse_of(原场次/素材版本)`，`body`: `{reuse_kind(replay|clip), billable:false}`。
  - 不变量：被 `reuse_of` 覆盖的素材段**不重复计酬**；复用方分账基数为新增价值，收入事件以 `links: reuse` 标注并在分账时排除复用部分。

### 3.5 收入确认与分账

- **CHANNEL_STATEMENT_RECEIVED** `channel_statement`：渠道单（广告分成等）。`body: `{period_start, period_end, line_items:[{version_ref, gross, deductions}], currency, statement_id}`。可能在周期封账后才到达。
- **REFUND_RECORDED** / **CHARGEBACK_RECORDED** `revenue_event`：退款、拒付。`links: original_payment`；按原消费路径**逆向冲减**（红冲），只影响仍有权利的贡献者；联合会员退款按未消费的递延余额退。
- **TAX_ASSESSED** `tax_line`：税费，借 `tax_payable` 相关科目，按法域归集。
- **REVENUE_ALLOCATED** `revenue_event`（历史事件，语义细化）：一笔收入归属完成。
  - `links`: `consumption`（或 `channel_statement`）、`version`、`adaptation`（若属改编）、`price_plan`、`deductions`（退款/拒付/税/暂扣）。
  - `subjects`：各贡献者按合同费率/角色、渠道、平台留存。
  - 创作者视图据此回答「这笔钱对应哪次消费、哪个版本、为什么被扣」。
- **REVENUE_REVERSED** `revenue_event`：红冲，`links: reverses`；用于退款拒付与更正，借贷方向与原事件相反。
- **DEFERRED_REVENUE_RECOGNIZED** `revenue_event`：预付会员收入随**实际消费**分期确认。`body: `{period, recognition_basis(actual_consumption), earned, remaining_deferred}`；借 `deferred_revenue`，贷 `revenue:*`。未消费的会员期留在 `deferred_revenue`，不确认。
- **ADJUSTMENT_POSTED** `revenue_event`：**迟到渠道单唯一合法入口**。`links: target_batch(已封账批次), statement`；`body: `{delta, reason}`。封账批次本身绝不重开，差额作为当期调整分录落入新批次，并在创作者视图标注「对 X 周期差额调整」。

### 3.6 争议与暂扣（隔离原则）

- **DISPUTE_OPENED** `revenue_event`（历史同名）：`body: `{scope:{work|version|contributor|territory}, reason}`。
- **WITHHOLDING_PLACED** `revenue_event`：争议款**暂存**。借相关贡献者 `payable`，贷 `withholding_escrow`。
  - 隔离不变量：暂扣只针对争议 `scope` 命中的主体与权利范围；**无争议贡献者的应付款照常进入 payout，不被拖住。**
- **DISPUTE_RESOLVED** / **WITHHOLDING_RELEASED**：争议结束，escrow 按结论付款（释放给贡献者）或红冲（返还平台/退回）。

### 3.7 结算批次

- **SETTLEMENT_OPENED** `settlement_batch`：开批，`body: `{period_start, period_end}`。
- **SETTLEMENT_CLOSED**（历史同名）：封账。封账后批次内分录冻结，迟到单据只能走 `ADJUSTMENT_POSTED`。
- **PAYOUT_SCHEDULED** `settlement_batch`：向贡献者排款；被冻结/暂扣范围之外的应付款照常排款。

## 4. 科目表（account）

| 科目 | 方向说明 |
| --- | --- |
| `cash_clearing` | 在途/待清分资金，资产，收款借记 |
| `deferred_revenue` | 预付/会员递延收入，负债，收款贷记、确认时借记 |
| `revenue:tips` / `revenue:unlock` / `revenue:subscription` / `revenue:ad_share` | 各媒介收入，确认时贷记 |
| `channel_payable` / `contributor_payable` / `platform_retained` | 渠道、贡献者应付与平台留存 |
| `channel_fee` | 渠道费（总额法下的费用，广告毛额确认时借记） |
| `tax_payable` | 应税负债 |
| `withholding_escrow` | 争议暂扣专户 |
| `refund_payable` / `chargeback_loss` | 退款应付 / 拒付损失 |
| `ar_adjustment` | 封账后差额调整的过渡科目 |

每个含 `effects` 的事件，必须**按币种各自借贷平衡**；整批重放后各科目期初+发生额得出期末，全币种试算平衡（借方合计=贷方合计）。

## 5. 全局不变量（验收断言）

1. **守恒**：单事件分录按币种平衡；重放一个（含已封账）批次，`Σ借=Σ贷`。
2. **封账不可变**：`SETTLEMENT_CLOSED` 后不得再有事件直接改写该批次分录；迟到单只产生 `ADJUSTMENT_POSTED`。
3. **递延随消费**：会员收入确认额 ≤ 当期实际消费对应的赚取额；未消费余额停留在递延。
4. **复用不重酬**：标 `reuse_of` 的素材段在派生版本分账基数中被排除一次且仅一次。
5. **范围最小约束**：修订/退出/冻结/下架只作用于其显式 `scope`；范围外收入与应付款不受影响。
6. **争议隔离**：`withholding_escrow` 只挂争议主体；无争议贡献者 `contributor_payable` 与 payout 不被延迟。
7. **可追溯**：任一 `revenue_event` 顺 `links` 可到达 consumption → version → work/adaptation → price_plan 与全部扣减原因。
8. **隐私最小化**：趋势投影只能读 `visibility=public_aggregates` 的聚合数据；不出现 `payment`/`entitlement` 明细、消费者标识、`confidential=true` 的单价。
