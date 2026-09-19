"""API 请求模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class TextIn(BaseModel):
    text: str = Field(default="", description="批量录入文本")


class ConfigIn(BaseModel):
    transfer_minutes: int = Field(gt=0, description="换乘有效间隔（分钟）")
    transfer_discount: float = Field(ge=0, description="换乘抵扣额（元）")
    daily_cap: float = Field(ge=0, description="每卡每运营日封顶额（元）")
    day_split_hour: int = Field(ge=0, le=23, description="运营日分界时刻")


class TrialConfigIn(BaseModel):
    """规则试算对比的草案参数。

    不在此处做数值约束：非法草案应走 core.validate_config，
    返回与正式核验一致的 CONFIG_BAD 定位错误（HTTP 422）。
    """
    transfer_minutes: float = Field(description="草案：换乘有效间隔（分钟）")
    transfer_discount: float = Field(description="草案：换乘抵扣额（元）")
    daily_cap: float = Field(description="草案：每卡每运营日封顶额（元）")
    day_split_hour: float = Field(description="草案：运营日分界时刻（0-23）")
