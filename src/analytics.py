"""趋势分析投影：只保留聚合安全字段。

趋势分析不得读取个人阅读轨迹或未公开单价。个人标识（user_id 等）一律
剔除；visibility 非 public 的价格方案不输出单价。受限键清单与
contracts/event-catalog.md 的敏感分级保持一致。
"""

PERSONAL_KEYS = ("user_id", "device_id", "account_id", "read_trace")


def sanitize_for_analytics(event: dict) -> dict:
    """返回剔除受限字段后的事件投影，原事件不被修改。"""
    projection = dict(event)
    payload = dict(projection.get("payload", {}))
    for key in PERSONAL_KEYS:
        payload.pop(key, None)
    if projection.get("event_type") == "PRICE_PLAN_PUBLISHED" and payload.get("visibility") != "public":
        payload.pop("unit_price", None)
    projection["payload"] = payload
    return projection


def trend_projection(events: list[dict]) -> list[dict]:
    """把事件流投影为趋势分析可读的形态。"""
    return [sanitize_for_analytics(event) for event in events]
