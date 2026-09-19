"""公交刷卡票价核验核心逻辑。

纯标准库实现：文本解析、记录校验、进出站配对、
换乘抵扣与运营日封顶计算。不依赖 FastAPI / SQLite，便于单独测试。

金额在内部统一以「元」的浮点数表示并保留两位小数；
存储层写入 SQLite 时转换为整数分，避免二进制浮点误差。
"""
from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

# 允许的文本分隔符：逗号、制表符、分号、空白(多空格)、全角逗号
_SPLIT_RE = re.compile(r"[,\t;，]|\s+")

# 日期与时刻之间可能用空格连接（如 "2026-09-18 08:00:00"），
# 而空格本身又是字段分隔符，切分前先把时间戳内部空白替换为 T。
# 形如：YYYY[-/]MM[-/]DD HH:MM[:SS] 或 MM[-/]DD HH:MM
_DATETIME_RE = re.compile(
    r"\d{2,4}[-/]\d{1,2}(?:[-/]\d{1,2})?[ \t]+\d{1,2}:\d{2}(?::\d{2})?"
)

# 支持的进站时刻格式
_TIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%dT%H:%M:%S",
    "%Y/%m/%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%m-%dT%H:%M",       # 缺省年份，补当前年
    "%m-%d %H:%M",
    "%m/%dT%H:%M",
    "%m/%d %H:%M",
)

ACTION_IN = "IN"
ACTION_OUT = "OUT"
_ACTION_ALIASES = {
    "IN": "IN", "进": "IN", "进站": "IN", "入": "IN", "上": "IN",
    "OUT": "OUT", "出": "OUT", "出站": "OUT", "下": "OUT",
}


def _err(code: str, message: str, line: Optional[int] = None,
         card: Optional[str] = None, time: Optional[str] = None,
         station: Optional[str] = None) -> dict:
    """构造定位错误对象。行号 / 卡号 / 时刻 / 站点尽量补齐，便于定位。"""
    loc: dict = {}
    if line is not None:
        loc["line"] = line
    if card:
        loc["card"] = card
    if time:
        loc["time"] = time
    if station:
        loc["station"] = station
    e: dict = {"code": code, "message": message}
    if loc:
        e["location"] = loc
    return e


def _split(line: str) -> list[str]:
    """按分隔符切分一行文本，去掉空白段。"""
    return [p for p in _SPLIT_RE.split(line.strip()) if p != ""]


def _round2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Station:
    name: str
    zone: int
    line: int = 1  # 保存时的原始行号，用于错误定位


@dataclass
class Fare:
    from_zone: int
    to_zone: int
    price: float
    line: int = 1


@dataclass
class Swipe:
    card: str
    t: datetime
    station: str
    action: str          # IN / OUT
    line: int = 1        # 文本行号（按卡号排序校验时用于定位）

    @property
    def time_text(self) -> str:
        return self.t.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Config:
    transfer_minutes: int = 90          # 换乘有效间隔（分钟）
    transfer_discount: float = 1.0      # 换乘抵扣额（元）
    daily_cap: float = 10.0             # 每卡每运营日封顶额（元）
    day_split_hour: int = 4             # 运营日分界时刻


@dataclass
class VerifyResult:
    ok: bool
    bills: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


@dataclass
class TrialResult:
    """规则试算对比结果。

    ok 为 False 时 current / draft 两个分块各自携带错误数组（原有定位错误）；
    ok 为 True 时携带对齐后的行程行 rows、按卡汇总 card_summaries 与
    全批次汇总 batch_summary。
    """
    ok: bool
    current: dict = field(default_factory=dict)
    draft: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    card_summaries: list[dict] = field(default_factory=list)
    batch_summary: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 文本解析（录入）
# ---------------------------------------------------------------------------

def parse_time(text: str, default_year: Optional[int] = None) -> datetime:
    """解析刷卡时刻，支持多种常见格式；失败抛 ValueError。"""
    text = text.strip()
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            dt = dt.replace(year=default_year or datetime.now().year)
        return dt
    raise ValueError(f"无法识别的时刻格式：{text}（示例 2026-09-18 08:00:00）")


def _parse_int(text: str, label: str) -> int:
    try:
        v = int(str(text).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是整数：{text!r}")
    return v


def _parse_amount(text: str, label: str = "金额") -> float:
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是数字：{text!r}")
    if math.isnan(v) or math.isinf(v) or v < 0:
        raise ValueError(f"{label}必须是不小于 0 的数字：{text!r}")
    return _round2(v)


def parse_stations_text(text: str) -> tuple[list[Station], list[dict]]:
    """站点分区文本，每行：站点名, 分区号。"""
    stations: list[Station] = []
    errors: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        if len(parts) != 2:
            errors.append(_err("STATION_BAD_FIELDS",
                               f"站点行应为「站点名, 分区号」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        name, zone_text = parts
        if name in seen:
            errors.append(_err("STATION_DUPLICATE",
                               f"站点 {name} 重复定义", line=i, station=name))
            continue
        try:
            zone = _parse_int(zone_text, "分区号")
        except ValueError as ex:
            errors.append(_err("STATION_BAD_ZONE", str(ex), line=i, station=name))
            continue
        stations.append(Station(name=name, zone=zone, line=i))
        seen.add(name)
    return stations, errors


def parse_fares_text(text: str) -> tuple[list[Fare], list[dict]]:
    """票价矩阵文本，每行：起始分区, 到达分区, 票价。"""
    fares: list[Fare] = []
    errors: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _split(line)
        if len(parts) != 3:
            errors.append(_err("FARE_BAD_FIELDS",
                               f"票价行应为「起始分区, 到达分区, 票价」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        fz_text, tz_text, price_text = parts
        try:
            fz = _parse_int(fz_text, "起始分区")
            tz = _parse_int(tz_text, "到达分区")
            price = _parse_amount(price_text, "票价")
        except ValueError as ex:
            errors.append(_err("FARE_BAD_VALUE", str(ex), line=i))
            continue
        key = (fz, tz)
        if key in seen:
            errors.append(_err("FARE_DUPLICATE",
                               f"分区 {fz}->{tz} 的票价重复定义", line=i))
            continue
        fares.append(Fare(from_zone=fz, to_zone=tz, price=price, line=i))
        seen.add(key)
    return fares, errors


def parse_records_text(text: str, default_year: Optional[int] = None
                       ) -> tuple[list[Swipe], list[dict]]:
    """刷卡记录文本，每行：卡号, 时刻, 站点, 进/出。

    保存阶段只做行格式解析；同卡配对、时间逆序、重复刷卡等
    跨记录的业务校验统一在 verify() 中完成，保证错误一次性报全。
    """
    records: list[Swipe] = []
    errors: list[dict] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 粘合时间戳内部的空白，避免被当作字段分隔符
        line = _DATETIME_RE.sub(lambda m: re.sub(r"[ \t]+", "T", m.group(0)), line)
        parts = _split(line)
        if len(parts) != 4:
            errors.append(_err("RECORD_BAD_FIELDS",
                               f"记录行应为「卡号, 时刻, 站点, 进/出」，实际 {len(parts)} 段：{line}",
                               line=i))
            continue
        card, time_text, station, action_text = parts
        card = card.strip()
        if not card:
            errors.append(_err("RECORD_BAD_CARD", "卡号不能为空", line=i))
            continue
        try:
            t = parse_time(time_text, default_year=default_year)
        except ValueError as ex:
            errors.append(_err("RECORD_BAD_TIME", str(ex),
                               line=i, card=card, station=station))
            continue
        action = _ACTION_ALIASES.get(action_text.upper())
        if action is None:
            errors.append(_err("RECORD_BAD_ACTION",
                               f"进出方向必须是 IN/进 或 OUT/出，实际：{action_text!r}",
                               line=i, card=card, time=time_text, station=station))
            continue
        records.append(Swipe(card=card, t=t, station=station,
                             action=action, line=i))
    return records, errors


# ---------------------------------------------------------------------------
# 运营日
# ---------------------------------------------------------------------------

def operating_day(t: datetime, split_hour: int = 4) -> str:
    """以某日 split_hour 点为界归属运营日。

    04:00 之前（含 00:00-03:59:59）归属前一自然日；
    04:00 及之后归属当日。例如 2026-09-18 03:30 属于 2026-09-17 运营日。
    """
    if t.hour < split_hour:
        t = t - timedelta(days=1)
    return t.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 配置校验
# ---------------------------------------------------------------------------

def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _is_int_value(x) -> bool:
    return _is_finite_number(x) and float(x).is_integer()


def validate_config(cfg: Config) -> list[dict]:
    errors: list[dict] = []
    if not _is_int_value(cfg.transfer_minutes) or cfg.transfer_minutes <= 0:
        errors.append(_err("CONFIG_BAD", "换乘有效间隔必须为正整数（分钟）"))
    if not _is_finite_number(cfg.transfer_discount) or cfg.transfer_discount < 0:
        errors.append(_err("CONFIG_BAD", "换乘抵扣额不能为负且必须是有限数字"))
    if not _is_finite_number(cfg.daily_cap) or cfg.daily_cap < 0:
        errors.append(_err("CONFIG_BAD", "每日封顶额不能为负且必须是有限数字"))
    if not _is_int_value(cfg.day_split_hour) or not (0 <= cfg.day_split_hour <= 23):
        errors.append(_err("CONFIG_BAD", "运营日分界时刻必须是 0-23 之间的整数小时"))
    return errors


# ---------------------------------------------------------------------------
# 进出站配对（verify 与票价矩阵路径审计共用）
# ---------------------------------------------------------------------------

def _pair_trips(records: list[Swipe]
                ) -> tuple[list[tuple[str, int, Swipe, Swipe]], list[dict]]:
    """按卡号分组、时间升序，做重复刷卡 / 时间逆序 / 进出站结构检查并配对。

    返回 (trips, errors)：trips 为 (卡号, 卡内序号(从1), 进站记录, 出站记录)；
    errors 为 DUPLICATE_SWIPE / TIME_REVERSED / MISSING_ENTRY / MISSING_EXIT
    定位错误。检查与报错顺序与 verify() 历史实现保持一致：
    先按卡号（首次出现顺序）逐卡报告重复刷卡、时间逆序，再做结构配对。
    """
    by_card: dict[str, list[Swipe]] = {}
    for r in records:
        by_card.setdefault(r.card, []).append(r)

    trips: list[tuple[str, int, Swipe, Swipe]] = []
    errors: list[dict] = []

    for card, rs in by_card.items():
        # 录入顺序（数据库按 id 返回，即文本录入先后）必须构成
        # 单调不减的时间流：下降=时间逆序；同卡同时刻=重复刷卡。
        ordered = True
        seen_times: dict[datetime, Swipe] = {}
        for r in rs:
            first = seen_times.get(r.t)
            if first is not None:
                errors.append(_err(
                    "DUPLICATE_SWIPE",
                    f"卡号 {card} 在 {r.time_text} 重复刷卡"
                    f"（第 {r.line} 行与第 {first.line} 行时刻完全相同）",
                    line=r.line, card=card, time=r.time_text,
                    station=r.station))
                ordered = False
            else:
                seen_times[r.t] = r
        for a, b in zip(rs, rs[1:]):
            if b.t < a.t:
                errors.append(_err(
                    "TIME_REVERSED",
                    f"卡号 {card} 刷卡时间逆序：第 {b.line} 行 {b.time_text} "
                    f"早于第 {a.line} 行 {a.time_text}",
                    line=b.line, card=card, time=b.time_text,
                    station=b.station))
                ordered = False

        # 存在逆序/重复时，进出站配对不可靠，跳过该卡结构检查
        # （结构异常很可能是乱序的假象，避免误报叠加）。
        if not ordered:
            continue

        # 时间流有序时录入顺序即时间顺序；逐对做进出站结构检查。
        # 期望从进站开始，进出成对；起始为出站=缺进站，末尾落单=缺出站。
        i, seq = 0, 0
        while i < len(rs):
            cur = rs[i]
            if cur.action != ACTION_IN:
                errors.append(_err(
                    "MISSING_ENTRY",
                    f"卡号 {card} 的出站刷卡缺少配对进站"
                    f"（{cur.time_text} @ {cur.station}）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            if i + 1 >= len(rs):
                errors.append(_err(
                    "MISSING_EXIT",
                    f"卡号 {card} 进站后缺失出站"
                    f"（{cur.time_text} @ {cur.station}）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            nxt = rs[i + 1]
            if nxt.action != ACTION_OUT:
                errors.append(_err(
                    "MISSING_EXIT",
                    f"卡号 {card} 在 {cur.time_text} @ {cur.station} 进站后，"
                    f"下一条不是出站（第 {nxt.line} 行又是进站）",
                    line=cur.line, card=card,
                    time=cur.time_text, station=cur.station))
                i += 1
                continue
            seq += 1
            trips.append((card, seq, cur, nxt))
            i += 2

    return trips, errors


# ---------------------------------------------------------------------------
# 核验 + 计价
# ---------------------------------------------------------------------------

def verify(stations: list[Station],
           fares: list[Fare],
           records: list[Swipe],
           cfg: Config) -> VerifyResult:
    """全量核验并计价。

    原子语义：任何记录级错误（缺出站、缺进站、时间逆序、重复刷卡、
    未知站点、矩阵缺票价）都会使整批核验失败，不生成任何账单。
    只有全部通过时才一次性产出全部行程账单。
    """
    errors: list[dict] = []
    errors.extend(validate_config(cfg))

    zone_of = {s.name: s.zone for s in stations}
    fare_of: dict[tuple[int, int], float] = {
        (f.from_zone, f.to_zone): f.price for f in fares
    }

    # 1) 站点引用检查（未知站点）
    for r in records:
        if r.station not in zone_of:
            errors.append(_err(
                "UNKNOWN_STATION",
                f"刷卡站点 {r.station} 未在站点分区中定义",
                line=r.line, card=r.card,
                time=r.time_text, station=r.station))

    # 2) 按卡号分组，时间升序，逐卡做重复刷卡 / 时间逆序 / 进出站结构检查并配对
    trips, struct_errors = _pair_trips(records)
    errors.extend(struct_errors)

    # 3) 票价矩阵覆盖检查（仅对结构完整、站点已知的行程）
    for card, seq, sw_in, sw_out in trips:
        z1 = zone_of.get(sw_in.station)
        z2 = zone_of.get(sw_out.station)
        if z1 is None or z2 is None:
            continue
        if (z1, z2) not in fare_of:
            errors.append(_err(
                "MISSING_FARE",
                f"票价矩阵缺少分区 {z1}->{z2}（{sw_in.station} -> "
                f"{sw_out.station}）的票价",
                line=sw_in.line, card=card, time=sw_in.time_text,
                station=sw_in.station))

    if errors:
        # 原子失败：不生成任何账单（无部分账单）
        return VerifyResult(ok=False, bills=[], errors=errors)

    # 5) 计价：按 (卡号, 进站时刻) 顺序处理。
    #    - 换乘抵扣看「同卡前次出站 -> 本次进站」的真实间隔（跨运营日也算）；
    #    - 封顶按行程进站时刻所属运营日、分卡累计。
    per_card_prev_out: dict[str, Swipe] = {}
    # 每卡每运营日累计实扣：card -> {day: amount}
    cumulative: dict[str, dict[str, float]] = {}

    bills: list[dict] = []
    ordered_trips = sorted(trips, key=lambda x: (x[0], x[2].t))

    for card, _seq, sw_in, sw_out in ordered_trips:
        z1 = zone_of[sw_in.station]
        z2 = zone_of[sw_out.station]
        base = _round2(fare_of[(z1, z2)])
        day = operating_day(sw_in.t, cfg.day_split_hour)

        discount = 0.0
        source = "无"
        prev_out = per_card_prev_out.get(card)
        if prev_out is not None:
            gap_min = (sw_in.t - prev_out.t).total_seconds() / 60.0
            if 0 <= gap_min <= cfg.transfer_minutes:
                discount = _round2(min(cfg.transfer_discount, base))
                source = (f"换乘优惠（距上次出站 {int(round(gap_min))} 分钟，"
                          f"≤{cfg.transfer_minutes} 分钟，抵扣 {discount:.2f} 元）")

        after_discount = _round2(base - discount)

        card_days = cumulative.setdefault(card, {})
        already = _round2(card_days.get(day, 0.0))
        cap_room = _round2(cfg.daily_cap - already)
        charge = _round2(max(0.0, min(after_discount, cap_room)))
        if cap_room <= 0:
            source = (f"{source}；当日已封顶" if discount > 0
                      else f"当日封顶（已累计 {already:.2f} 元，达到 "
                           f"{cfg.daily_cap:.2f} 元）")
        elif charge < after_discount:
            source = (f"{source}；封顶保护" if discount > 0
                      else f"封顶保护（当日封顶 {cfg.daily_cap:.2f} 元）")

        card_days[day] = _round2(already + charge)
        per_card_prev_out[card] = sw_out

        bills.append({
            "card": card,
            "seq": None,  # 下方按全局展示顺序统一编号
            "operating_day": day,
            "entry_time": sw_in.time_text,
            "exit_time": sw_out.time_text,
            "entry_station": sw_in.station,
            "exit_station": sw_out.station,
            "from_zone": z1,
            "to_zone": z2,
            "base_fare": base,
            "discount": discount,
            "discount_source": source,
            "charged_fare": charge,
            "daily_cumulative": card_days[day],
            "daily_cap": _round2(cfg.daily_cap),
            "entry_line": sw_in.line,
            "exit_line": sw_out.line,
        })

    # 同一卡号内按进站顺序给出展示序号
    seq_by_card: dict[str, int] = {}
    for b in bills:
        seq_by_card[b["card"]] = seq_by_card.get(b["card"], 0) + 1
        b["seq"] = seq_by_card[b["card"]]

    return VerifyResult(ok=True, bills=bills, errors=[])


# ---------------------------------------------------------------------------
# 配对修复预览（只读：不改记录、不写账单）
# ---------------------------------------------------------------------------

# 修复预览只处理进出站配对结构问题；其余错误（未知站点、缺票价、
# 时间逆序、重复刷卡、参数非法）一经出现即按原样返回，不生成修复方案。
_REPAIRABLE_CODES = {"MISSING_ENTRY", "MISSING_EXIT"}

# 允许的编辑操作（各计 1 次编辑）
OP_KEEP = "keep"      # 原样保留
OP_FLIP = "flip"      # 翻转进/出方向
OP_DELETE = "delete"  # 删除该条记录

# 同成本方案最多返回条数
REPAIR_MAX_PLANS = 5


@dataclass
class RepairPreview:
    """配对修复预览结果。

    ok=False 时 errors 为 verify() 的原有定位错误（未知站点、缺票价、
    时间逆序、重复刷卡等非配对错误原样返回，不生成修复方案）。
    ok=True 时 plans 为最小编辑方案（按稳定顺序，最多 REPAIR_MAX_PLANS 个），
    min_edits 为全局最小编辑数，total_plans 为同成本方案总数，
    hidden_plans 为未展示的条数；min_edits=0 时 plans 为空（无需修复）。
    """
    ok: bool
    min_edits: int = 0
    total_plans: int = 0
    shown_plans: int = 0
    hidden_plans: int = 0
    plans: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


def _flip_action(action: str) -> str:
    return ACTION_OUT if action == ACTION_IN else ACTION_IN


def _min_edit_solutions(swipes: list[Swipe]
                        ) -> tuple[int, int, list[list[str]]]:
    """单卡（已按时间升序）最小编辑修复：动态规划。

    每条记录可 keep（0 编辑，方向须与期待一致）、flip（1 编辑，
    翻转后须与期待一致）或 delete（1 编辑）；保留记录的方向序列
    必须从进站开始严格交替（即可完整配对）。

    返回 (最小编辑数, 最小编辑方案总数, 按稳定顺序的前
    REPAIR_MAX_PLANS 个操作序列)。稳定顺序为 DFS 字典序
    （keep < flip < delete，靠前的记录优先被编辑）。
    """
    n = len(swipes)
    INF = n + 1
    # f[i][p]：处理第 i..n-1 条、期待方向 p（0=期待进站，1=期待出站）
    # 所需的最小编辑数；g[i][p] 为达到该最小值的方案数。
    f = [[INF, INF] for _ in range(n + 1)]
    f[n][0] = 0
    for i in range(n - 1, -1, -1):
        a = swipes[i].action
        for p in (0, 1):
            best = INF
            if (p == 0) == (a == ACTION_IN):              # keep
                best = min(best, f[i + 1][1 - p])
            if (p == 0) == (_flip_action(a) == ACTION_IN):  # flip
                best = min(best, 1 + f[i + 1][1 - p])
            best = min(best, 1 + f[i + 1][p])             # delete
            f[i][p] = best
    g = [[0, 0] for _ in range(n + 1)]
    g[n][0] = 1
    for i in range(n - 1, -1, -1):
        a = swipes[i].action
        for p in (0, 1):
            total = 0
            if (p == 0) == (a == ACTION_IN) \
                    and f[i + 1][1 - p] == f[i][p]:
                total += g[i + 1][1 - p]
            if (p == 0) == (_flip_action(a) == ACTION_IN) \
                    and 1 + f[i + 1][1 - p] == f[i][p]:
                total += g[i + 1][1 - p]
            if 1 + f[i + 1][p] == f[i][p]:
                total += g[i + 1][p]
            g[i][p] = total

    # 沿最优边按稳定顺序物化前 REPAIR_MAX_PLANS 个操作序列
    plans: list[list[str]] = []

    def walk(i: int, p: int, ops: list[str]) -> None:
        if len(plans) >= REPAIR_MAX_PLANS:
            return
        if i == n:
            plans.append(list(ops))
            return
        a = swipes[i].action
        if (p == 0) == (a == ACTION_IN) and f[i + 1][1 - p] == f[i][p]:
            ops.append(OP_KEEP)
            walk(i + 1, 1 - p, ops)
            ops.pop()
        if (p == 0) == (_flip_action(a) == ACTION_IN) \
                and 1 + f[i + 1][1 - p] == f[i][p]:
            ops.append(OP_FLIP)
            walk(i + 1, 1 - p, ops)
            ops.pop()
        if 1 + f[i + 1][p] == f[i][p]:
            ops.append(OP_DELETE)
            walk(i + 1, p, ops)
            ops.pop()

    walk(0, 0, [])
    return f[0][0], g[0][0], plans


def _build_repair_plan(by_card: dict[str, list[Swipe]],
                       card_plans: dict[str, list[list[str]]],
                       choices: dict[str, int],
                       min_edits: int) -> dict:
    """按各卡选定的操作序列，组装一个完整修复方案。

    方案按卡号排序、卡内按时间顺序逐条给出变更（删除 / 翻转方向），
    并生成修复后的完整记录文本（按卡号 + 时刻排序，方向写作 进/出）。
    """
    changes: list[dict] = []
    fixed: list[Swipe] = []
    for card in sorted(by_card):
        swipes = by_card[card]
        ops = card_plans[card][choices[card]]
        for r, op in zip(swipes, ops):
            if op == OP_DELETE:
                changes.append({
                    "op": OP_DELETE,
                    "card": r.card, "time": r.time_text,
                    "station": r.station, "line": r.line,
                    "from_action": r.action, "to_action": None,
                    "detail": f"删除记录：{r.card} {r.time_text} "
                              f"@ {r.station}（原方向 {r.action}）",
                })
            elif op == OP_FLIP:
                na = _flip_action(r.action)
                changes.append({
                    "op": OP_FLIP,
                    "card": r.card, "time": r.time_text,
                    "station": r.station, "line": r.line,
                    "from_action": r.action, "to_action": na,
                    "detail": f"翻转方向：{r.card} {r.time_text} @ {r.station}"
                              f"（{r.action} → {na}）",
                })
                fixed.append(Swipe(card=r.card, t=r.t, station=r.station,
                                   action=na, line=r.line))
            else:
                fixed.append(r)

    fixed.sort(key=lambda r: (r.card, r.t))
    lines = [f"{r.card}, {r.time_text}, {r.station}, "
             f"{'进' if r.action == ACTION_IN else '出'}" for r in fixed]
    return {
        "edit_count": min_edits,
        "changes": changes,
        "change_count": len(changes),
        "kept_count": len(fixed),
        "trip_count": len(fixed) // 2,
        "fixed_text": "\n".join(lines),
    }


def repair_preview(stations: list[Station],
                   fares: list[Fare],
                   records: list[Swipe],
                   cfg: Config) -> RepairPreview:
    """对已保存刷卡记录生成「可完整配对」的最小编辑修复预览。

    - 先用 verify() 全量校验：通过则无需修复；存在非配对错误
      （未知站点、缺票价、时间逆序、重复刷卡、参数非法）时按原错误返回；
    - 仅针对缺失进站 / 缺失出站 / 连续同向刷卡（verify 的 MISSING_ENTRY /
      MISSING_EXIT），按卡号分组、卡内按时间升序，做全局最小编辑搜索；
    - 允许的编辑只有「翻转进/出方向」与「删除记录」，各计 1 次；
      原始时刻、站点、卡号一律不改写；
    - 同成本方案按稳定顺序最多返回 REPAIR_MAX_PLANS 个，并标明
      最小编辑数与未展示的方案数量，不替用户选定任何方案；
    - 全程只读：不保存记录、不写/清空账单。
    """
    result = verify(stations, fares, records, cfg)
    if result.ok:
        return RepairPreview(ok=True)  # 记录已可完整配对，无需修复

    blocking = [e for e in result.errors
                if e.get("code") not in _REPAIRABLE_CODES]
    if blocking:
        # 非配对错误原样返回，不生成修复方案
        return RepairPreview(ok=False, errors=result.errors)

    # 按卡号分组，卡内按时间升序（verify 已确认有序，即录入顺序；
    # 此处显式排序使预览不依赖录入顺序）
    by_card: dict[str, list[Swipe]] = {}
    for r in records:
        by_card.setdefault(r.card, []).append(r)
    for rs in by_card.values():
        rs.sort(key=lambda r: r.t)

    # 各卡独立求最小编辑；全局最小编辑数 = 各卡之和，
    # 同成本方案总数 = 各卡方案数之积（卡间相互独立）。
    card_plans: dict[str, list[list[str]]] = {}
    min_edits = 0
    total = 1
    for card in sorted(by_card):
        cost, count, first = _min_edit_solutions(by_card[card])
        card_plans[card] = first
        min_edits += cost
        total *= count

    shown = min(total, REPAIR_MAX_PLANS)
    plans: list[dict] = []
    # 稳定顺序枚举方案组合：混合进制计数，卡号大的方案索引变化最快；
    # 全局前 REPAIR_MAX_PLANS 个组合只依赖各卡物化的前 REPAIR_MAX_PLANS 个序列。
    cards = sorted(by_card)
    for combo in range(shown):
        choices: dict[str, int] = {}
        rest = combo
        for card in reversed(cards):
            choices[card] = rest % len(card_plans[card])
            rest //= len(card_plans[card])
        plans.append(_build_repair_plan(by_card, card_plans,
                                        choices, min_edits))

    return RepairPreview(
        ok=True,
        min_edits=min_edits,
        total_plans=total,
        shown_plans=len(plans),
        hidden_plans=total - len(plans),
        plans=plans,
    )


# ---------------------------------------------------------------------------
# 规则试算对比（不落库、不改配置）
# ---------------------------------------------------------------------------

def _snap_int(x) -> int | float:
    """整数型数字快照为 int（如 120.0 -> 120），非整数保留原值。"""
    return int(x) if _is_finite_number(x) and float(x).is_integer() else x


def _config_snapshot(cfg: Config) -> dict:
    return {
        "transfer_minutes": _snap_int(cfg.transfer_minutes),
        "transfer_discount": _round2(cfg.transfer_discount),
        "daily_cap": _round2(cfg.daily_cap),
        "day_split_hour": _snap_int(cfg.day_split_hour),
    }


def _index_bills(bills: list[dict]) -> dict[tuple[str, int], dict]:
    return {(b["card"], b["seq"]): b for b in bills}


def trial_compare(stations: list[Station],
                  fares: list[Fare],
                  records: list[Swipe],
                  current_cfg: Config,
                  draft_cfg: Config) -> TrialResult:
    """用同一批已保存的刷卡记录，分别按当前配置与草案配置试算计费。

    - 完全复用 verify() 的解析后配对 / 校验 / 计价规则，本函数不做任何写操作；
    - 任一方案失败（配置参数非法或数据错误）时，返回该方案 verify() 产出的
      原有定位错误，不产出对比账单；
    - 两方案都成功时，按 (卡号, 卡内序号) 对齐行程，逐程给出运营日、
      优惠、实扣及差额，并汇总每张卡与全批次的金额变化。
    """
    current = verify(stations, fares, records, current_cfg)
    draft = verify(stations, fares, records, draft_cfg)

    if not current.ok or not draft.ok:
        return TrialResult(
            ok=False,
            current={"config": _config_snapshot(current_cfg),
                     "ok": current.ok,
                     "errors": current.errors},
            draft={"config": _config_snapshot(draft_cfg),
                   "ok": draft.ok,
                   "errors": draft.errors},
        )

    cur_map = _index_bills(current.bills)
    draft_map = _index_bills(draft.bills)
    # 配对与卡内序号仅取决于记录数据本身，两边应当一致；用并集以防万一。
    keys = sorted(set(cur_map) | set(draft_map))

    rows: list[dict] = []
    for card, seq in keys:
        cb = cur_map.get((card, seq))
        db = draft_map.get((card, seq))

        def pick(key: str):
            return (cb[key] if cb is not None else None,
                    db[key] if db is not None else None)

        cur_day, draft_day = pick("operating_day")
        cur_disc, draft_disc = pick("discount")
        cur_charge, draft_charge = pick("charged_fare")
        rows.append({
            "card": card,
            "seq": seq,
            "entry_time": (cb or db)["entry_time"],
            "exit_time": (cb or db)["exit_time"],
            "entry_station": (cb or db)["entry_station"],
            "exit_station": (cb or db)["exit_station"],
            "from_zone": (cb or db)["from_zone"],
            "to_zone": (cb or db)["to_zone"],
            "base_fare": (cb or db)["base_fare"],
            "current_operating_day": cur_day,
            "draft_operating_day": draft_day,
            "day_changed": cur_day != draft_day,
            "current_discount": cur_disc,
            "draft_discount": draft_disc,
            "discount_diff": _round2((draft_disc or 0) - (cur_disc or 0)),
            "current_charge": cur_charge,
            "draft_charge": draft_charge,
            "charge_diff": _round2((draft_charge or 0) - (cur_charge or 0)),
            "draft_discount_source": db["discount_source"] if db else None,
            "current_discount_source": cb["discount_source"] if cb else None,
        })

    card_summaries: list[dict] = []
    for card in sorted({k[0] for k in keys}):
        cb_sum = sum(b["charged_fare"] for b in current.bills if b["card"] == card)
        db_sum = sum(b["charged_fare"] for b in draft.bills if b["card"] == card)
        cd_sum = sum(b["discount"] for b in current.bills if b["card"] == card)
        dd_sum = sum(b["discount"] for b in draft.bills if b["card"] == card)
        card_summaries.append({
            "card": card,
            "trip_count": sum(1 for k in keys if k[0] == card),
            "current_charge_total": _round2(cb_sum),
            "draft_charge_total": _round2(db_sum),
            "charge_diff": _round2(db_sum - cb_sum),
            "current_discount_total": _round2(cd_sum),
            "draft_discount_total": _round2(dd_sum),
            "discount_diff": _round2(dd_sum - cd_sum),
        })

    cur_total = sum(b["charged_fare"] for b in current.bills)
    draft_total = sum(b["charged_fare"] for b in draft.bills)
    cur_disc_total = sum(b["discount"] for b in current.bills)
    draft_disc_total = sum(b["discount"] for b in draft.bills)
    batch_summary = {
        "trip_count": len(keys),
        "current_charge_total": _round2(cur_total),
        "draft_charge_total": _round2(draft_total),
        "charge_diff": _round2(draft_total - cur_total),
        "current_discount_total": _round2(cur_disc_total),
        "draft_discount_total": _round2(draft_disc_total),
        "discount_diff": _round2(draft_disc_total - cur_disc_total),
    }

    return TrialResult(
        ok=True,
        current={"config": _config_snapshot(current_cfg),
                 "ok": True,
                 "errors": [],
                 "bill_count": len(current.bills)},
        draft={"config": _config_snapshot(draft_cfg),
               "ok": True,
               "errors": [],
               "bill_count": len(draft.bills)},
        rows=rows,
        card_summaries=card_summaries,
        batch_summary=batch_summary,
    )


# ---------------------------------------------------------------------------
# 票价矩阵路径审计（只读：不改矩阵、不写账单）
# ---------------------------------------------------------------------------

@dataclass
class FareAuditResult:
    """票价矩阵路径审计结果。

    ok=False 时 errors 为阻断性错误（CONFIG_BAD / UNKNOWN_STATION /
    DUPLICATE_SWIPE / TIME_REVERSED / MISSING_ENTRY / MISSING_EXIT）的
    原有定位错误，不产出审计结论。
    ok=True 时：
    - suggestions：缺少直达票价但存在可达（多跳/零价环）路径的分区对，
      给出建议票价、完整路径与受影响行程（卡号 + 原始行号）；
    - overpriced：已有直达票价高于间接最低成本路径的分区对，标出差额；
    - missing_errors：实际行程需要但既无直达票价也不可达的分区对，
      沿用 verify() 的 MISSING_FARE 定位错误（每程一条）；
    - direct_ok：实际行程中直达票价不高于间接路径的分区对数。
    """
    ok: bool
    suggestions: list[dict] = field(default_factory=list)
    overpriced: list[dict] = field(default_factory=list)
    missing_errors: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    pair_count: int = 0
    direct_ok_count: int = 0


def _build_adjacency(stations: list[Station], fares: list[Fare]
                     ) -> tuple[list[int], dict[int, list[tuple[int, float]]]]:
    """构造有向图：分区为节点、已配置的有向票价为边。

    节点集合 = 站点分区 ∪ 票价矩阵中出现的分区；同一对分区票价唯一
    （录入阶段已去重），故每对节点至多一条边。
    """
    zones = {s.zone for s in stations}
    for f in fares:
        zones.add(f.from_zone)
        zones.add(f.to_zone)
    nodes = sorted(zones)
    adj: dict[int, list[tuple[int, float]]] = {z: [] for z in nodes}
    for f in fares:
        adj[f.from_zone].append((f.to_zone, f.price))
    for edges in adj.values():
        edges.sort(key=lambda e: e[0])
    return nodes, adj


def _shortest_paths(src: int,
                    adj: dict[int, list[tuple[int, float]]]
                    ) -> dict[int, tuple[float, list[int]]]:
    """从 src 出发的最低成本路径（Dijkstra）。

    - 边权非负（允许 0 价边）；
    - 相同总价时按「分区序列」字典序稳定选路（路径为简单路径，
      0 价绕行环不会被选入，因为相同成本下更短/字典序更小的路径占优）；
    - 仅使用有向边，方向非对称时分别计算。

    返回 dst -> (最低成本, 分区序列[含 src 与 dst])，dst=src 时表示回到本
    分区的非空简单路径（自环或绕行环），用于同分区进出的行程审计。
    """
    # 标签为 (成本, 路径序列)：先比成本，成本相同比序列字典序。
    best: dict[int, tuple[float, list[int]]] = {}
    heap: list[tuple[float, list[int]]] = []
    # 自环 src->src 初始即为 [src, src]，弹出时按「回到起点」分支参与比较。
    for nbr, w in adj.get(src, ()):
        heapq.heappush(heap, (_round2(w), [src, nbr]))

    while heap:
        cost, path = heapq.heappop(heap)
        dst = path[-1]
        if dst == src:
            # 回到起点：候选环路径，不继续扩展（再走会重复 src）。
            if src not in best or (cost, path) < best[src]:
                best[src] = (cost, path)
            continue
        old = best.get(dst)
        if old is not None:
            if (cost, path) >= old:
                continue
        best[dst] = (cost, path)
        visited = set(path)
        for nbr, w in adj.get(dst, ()):
            if nbr in visited:
                # 保持路径为简单路径；经已访问节点（含 src）的边不展开，
                # 回到 src 的环由该节点自身的入边候选覆盖。
                if nbr == src:
                    cand = (_round2(cost + w), path + [src])
                    if src not in best or cand < best[src]:
                        best[src] = cand
                continue
            heapq.heappush(heap, (_round2(cost + w), path + [nbr]))

    return best


def _path_hops(path: list[int], fare_of: dict[tuple[int, int], float]
               ) -> list[dict]:
    """把分区序列展开为逐跳明细（每跳都是矩阵中已配置的有向票价）。"""
    hops = []
    for a, b in zip(path, path[1:]):
        hops.append({"from_zone": a, "to_zone": b,
                     "price": _round2(fare_of[(a, b)])})
    return hops


def _missing_fare_error(o: dict, z1: int, z2: int) -> dict:
    """与 verify() 完全一致的 MISSING_FARE 定位错误（沿用原定位信息）。"""
    return _err(
        "MISSING_FARE",
        f"票价矩阵缺少分区 {z1}->{z2}（{o['entry_station']} -> "
        f"{o['exit_station']}）的票价",
        line=o["entry_line"], card=o["card"], time=o["entry_time"],
        station=o["entry_station"])


def fare_audit(stations: list[Station],
               fares: list[Fare],
               records: list[Swipe],
               cfg: Config) -> FareAuditResult:
    """票价矩阵路径审计（只读）。

    复用已保存的站点、票价与刷卡记录：以分区为节点、已配置的有向票价
    为边，对每张卡实际行程的分区对求最低成本路径。

    - 缺少直达票价但存在可达路径 → suggestions：建议票价（路径总价）、
      完整路径（分区序列 + 逐跳票价）、受影响卡号与原始行号，并给出可
      直接补录进票价矩阵的建议行文本；
    - 已有直达票价但高于间接路径 → overpriced：标出差额（直达 − 间接），
      并给出建议改价行；相同总价按分区序列稳定选路（直达边序列为
      [z1, z2]，字典序最小，故等价时不会误报）；
    - 不可达（也没有回到本分区的非空路径）→ 沿用 MISSING_FARE 原定位
      错误（missing_errors，按实际行程逐程给出）；
    - 其他数据错误（未知站点、重复刷卡、时间逆序、缺进/出站、配置非法）
      为阻断性错误，原样返回，不产出任何审计结论。

    审计全程只读：不修改矩阵或账单。
    """
    errors: list[dict] = []
    errors.extend(validate_config(cfg))

    zone_of = {s.name: s.zone for s in stations}
    fare_of: dict[tuple[int, int], float] = {
        (f.from_zone, f.to_zone): f.price for f in fares
    }

    for r in records:
        if r.station not in zone_of:
            errors.append(_err(
                "UNKNOWN_STATION",
                f"刷卡站点 {r.station} 未在站点分区中定义",
                line=r.line, card=r.card,
                time=r.time_text, station=r.station))

    trips, struct_errors = _pair_trips(records)
    errors.extend(struct_errors)
    if errors:
        return FareAuditResult(ok=False, errors=errors)

    _, adj = _build_adjacency(stations, fares)

    # 实际行程按分区对归组（按卡号 + 进站时刻稳定排序），保留每程定位信息。
    groups: dict[tuple[int, int], list[dict]] = {}
    for card, _seq, sw_in, sw_out in sorted(trips, key=lambda x: (x[0], x[2].t)):
        z1, z2 = zone_of[sw_in.station], zone_of[sw_out.station]
        groups.setdefault((z1, z2), []).append({
            "card": card,
            "entry_time": sw_in.time_text,
            "exit_time": sw_out.time_text,
            "entry_station": sw_in.station,
            "exit_station": sw_out.station,
            "entry_line": sw_in.line,
            "exit_line": sw_out.line,
        })

    suggestions: list[dict] = []
    overpriced: list[dict] = []
    unreachable: set[tuple[int, int]] = set()
    direct_ok = 0

    # 每个起点只跑一次 Dijkstra（同一起点的多个分区对复用结果）。
    shortest_cache: dict[int, dict[int, tuple[float, list[int]]]] = {}

    def occurrence_lines(occ: list[dict]) -> str:
        return "、".join(f"{o['card']}（进第 {o['entry_line']} 行/"
                         f"出第 {o['exit_line']} 行）" for o in occ)

    for (z1, z2), occ in sorted(groups.items()):
        table = shortest_cache.get(z1)
        if table is None:
            table = _shortest_paths(z1, adj)
            shortest_cache[z1] = table
        best = table.get(z2)

        direct = fare_of.get((z1, z2))
        if best is None:
            # 不可达：先记下分区对，稍后按实际行程顺序逐程给出
            # MISSING_FARE（与 verify 的报错顺序、定位信息一致）。
            unreachable.add((z1, z2))
            continue

        best_cost, path = best
        best_cost = _round2(best_cost)
        hops = _path_hops(path, fare_of)
        path_text = " → ".join(
            f"{h['from_zone']}→{h['to_zone']}({h['price']:.2f}元)" for h in hops)

        if direct is None:
            # 缺直达票价但可达：给出建议补录行。
            suggestions.append({
                "from_zone": z1,
                "to_zone": z2,
                "suggested_fare": best_cost,
                "hop_count": len(hops),
                "path_zones": path,
                "hops": hops,
                "path_text": path_text,
                "affected_cards": sorted({o["card"] for o in occ}),
                "affected_count": len(occ),
                "occurrences": occ,
                "affected_text": occurrence_lines(occ),
                "suggested_line": f"{z1}, {z2}, {best_cost:.2f}",
            })
        else:
            direct = _round2(direct)
            if direct > best_cost:
                diff = _round2(direct - best_cost)
                overpriced.append({
                    "from_zone": z1,
                    "to_zone": z2,
                    "direct_fare": direct,
                    "indirect_fare": best_cost,
                    "diff": diff,
                    "hop_count": len(hops),
                    "path_zones": path,
                    "hops": hops,
                    "path_text": path_text,
                    "affected_cards": sorted({o["card"] for o in occ}),
                    "affected_count": len(occ),
                    "occurrences": occ,
                    "affected_text": occurrence_lines(occ),
                    "suggested_line": f"{z1}, {z2}, {best_cost:.2f}",
                })
            else:
                direct_ok += 1

    # 不可达分区对：按实际行程顺序（卡号分组、卡内时间升序）逐程给出
    # 与 verify() 完全一致的 MISSING_FARE 定位错误，保持原报错顺序。
    missing_errors: list[dict] = []
    for card, _seq, sw_in, sw_out in trips:
        z1, z2 = zone_of[sw_in.station], zone_of[sw_out.station]
        if (z1, z2) in unreachable:
            missing_errors.append(_missing_fare_error(
                {"card": card, "entry_line": sw_in.line,
                 "entry_time": sw_in.time_text,
                 "entry_station": sw_in.station,
                 "exit_station": sw_out.station}, z1, z2))

    return FareAuditResult(
        ok=True,
        suggestions=suggestions,
        overpriced=overpriced,
        missing_errors=missing_errors,
        pair_count=len(groups),
        direct_ok_count=direct_ok,
    )
