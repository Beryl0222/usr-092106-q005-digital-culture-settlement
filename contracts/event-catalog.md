# 事件目录

本目录逐类说明领域事件的 payload 约定、敏感分级与领域规则，配合
`contracts/domain.schema.json`（信封与词汇）使用。

## 通用约定

- **金额**：一律为最小货币单位的整数（CNY 为分），字段名 `amount` / `unit_price` /
  `disputed_amount`。同一结算批次内币种一致。
- **时间**：ISO 8601 带时区，全库统一 `+08:00` 表示，字符串可直接比较先后。
- **标识**：`contract_id`、`rights_term_id`、`asset_version_id` 等为既有契约标识，
  后续事件原样引用，不重复登记合同本体。事件一旦被接收，其标识、发生时间和版本
  不被原地改写；业务更正产生后继记录（差额调整、冲减、暂存释放）。
- **批次归属**：入账事件以 `payload.settlement_batch_id` 归属结算批次；未确认的
  递延、登记类事件不带批次。
- **payload 扁平**：不嵌套个人信息结构，受限键见下表。

## 敏感分级

| 级别 | 字段 | 约束 |
| --- | --- | --- |
| personal（个人轨迹） | `user_id`、`device_id`、`account_id`、`read_trace` | 趋势分析禁读 |
| commercial（商业敏感） | `visibility != "public"` 价格方案的 `unit_price` | 趋势分析禁读 |

## 事件表

| event_type | 聚合 | 关键 payload | 语义与约束 |
| --- | --- | --- | --- |
| ASSET_REGISTERED | content_asset | title, medium | 登记原始作品（连载网文 / 按集短剧 / 非遗直播） |
| ASSET_VERSION_PUBLISHED | content_asset | work_id, kind, label | 发布章节、剧集或场次版本；消费与分账均引用版本 id |
| ADAPTATION_LINKED | content_asset | source_asset_id, contract_id | 登记改编关系；跨媒介分账以 contract_id 为准 |
| CONTRIBUTOR_REGISTERED | contributor | name, role, contract_id | 登记贡献者（作者 / 主播 / 制作方）及其契约标识 |
| RIGHT_REGISTERED | rights_term | asset_id, right_kind, territory, term_start, term_end, contract_id | 登记地域与期限权利 |
| RIGHTS_SCOPE_CONSTRAINED | rights_term | reason, rights_term_ids, effective_from | 合同修订 / 作者退出 / 侵权冻结 / 内容下架；仅约束列明的权利项，自 effective_from 生效 |
| PRICE_PLAN_PUBLISHED | price_plan | kind, unit_price, currency, visibility | 价格方案；未公开单价见敏感分级 |
| PAYMENT_RECEIVED | payment | user_id, amount, currency, kind, price_plan_id | 用户支付（打赏 / 解锁 / 订阅 / 联合会员） |
| REFUND_RECORDED | payment | amount, deferred_from 或 reverses_recognition | 退款；只有冲减已确认收入时才进入批次账，未消费递延的退款只影响递延余额 |
| CHARGEBACK_RECORDED | payment | amount, reverses_recognition, settlement_batch_id | 拒付，冲减已确认收入 |
| CHANNEL_STATEMENT_RECEIVED | channel_statement | channel, amount, currency, revenue_kind, statement_period, final | 渠道单登记（广告分成等） |
| LATE_STATEMENT_ADJUSTED | channel_statement | adjusts_batch_id, settlement_batch_id, amount | 迟到渠道单的差额调整：必须指向已封账批次，入账到开放批次 |
| CONSUMPTION_RECORDED | revenue_event | user_id, asset_version_id, kind, reuse_of? | 消费记录；`reuse_of` 标记复用素材（直播回放、短剧剪辑） |
| REVENUE_DEFERRED | revenue_event | payment_id, amount, currency | 预付会员收入递延，不计入批次 |
| REVENUE_RECOGNIZED | revenue_event | consumption_id / statement_id / payment_id（三选一）, amount, revenue_kind, asset_version_id, rights_term_id, contract_id, settlement_batch_id | 收入确认；预付类另带 deferred_from |
| REVENUE_ALLOCATED | revenue_event | contributor_id, amount, contract_id, rights_term_id, source_ids, asset_version_id, settlement_batch_id | 分账到贡献者，金额为净应付；source_ids 回溯确认事件 |
| DEDUCTION_APPLIED | revenue_event | reason, amount, applies_to, settlement_batch_id | 扣减（渠道服务费 / 平台服务费 / 税费代扣），原因对创作者可见 |
| DISPUTE_OPENED | dispute_case | parties, subject, disputed_amount | 争议登记，parties 为争议当事方 |
| DISPUTE_RESOLVED | dispute_case | resolution | 争议结案 |
| ESCROW_HELD | dispute_case | dispute_id, escrow_id, contributor_id, amount, settlement_batch_id | 争议款暂存，仅限争议当事方 |
| ESCROW_RELEASED | dispute_case | dispute_id, escrow_id, contributor_id, amount, settlement_batch_id | 暂存释放，计入释放所在批次的流入 |
| SETTLEMENT_BATCH_OPENED | settlement_batch | period_start, period_end | 结算批次开启 |
| SETTLEMENT_CLOSED | settlement_batch | period_end | 批次封账；此后仅接受差额调整 |

## 领域规则

以下规则由 `src/rules.py` 与 `src/ledger.py` 执行，事件流须按发生时间排序。

- **R1 守恒**：每个结算批次、每个币种满足
  `确认收入 + 差额调整 + 暂存释放 − 退款拒付冲减 = 分账应付 + 争议暂存 + 各项扣减`。
  财务按批次重放全部入账事件即可验证。
- **R2 分期确认**：预付会员收入先经 REVENUE_DEFERRED 递延，随实际消费以
  REVENUE_RECOGNIZED 分期确认；同一笔支付的累计确认加退款不得超过其递延总额。
- **R3 不重复计酬**：同一笔消费只能确认一次收入；带 `reuse_of` 的复用素材消费
  （直播回放、短剧剪辑）不再产生按次计酬，其收入经渠道单广告分成进入。
- **R4 已封账周期**：批次封账后，指向该批次的入账事件只接受
  LATE_STATEMENT_ADJUSTED，且调整必须入账到未封账批次。
- **R5 争议隔离**：ESCROW_HELD 的贡献者必须是争议当事方，争议结案前不得新增
  暂存；无争议贡献者的分账在同一批次正常入账，不被拖住。
- **R6 权利范围约束**：RIGHTS_SCOPE_CONSTRAINED 生效后，受限 `rights_term_id`
  项下不得再产生分账；未列明的权利项不受影响。

## 创作者视角的回溯链

一笔分账（REVENUE_ALLOCATED）经 `source_ids` 指向确认事件，确认事件经
`consumption_id` / `statement_id` 指向消费或渠道单，消费与确认事件携带
`asset_version_id` 定位作品版本；DEDUCTION_APPLIED 经 `applies_to` 挂在分账或
确认上并注明 `reason`。创作者由此看见一笔收入对应的消费、作品版本和扣减原因。
