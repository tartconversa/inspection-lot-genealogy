"""追溯仓核心数据模型。

所有记录只描述“已经发生的事实”，不携带处置结论；处置结论由
:mod:`traceware.engine` 在特定缺陷通告版本与证据截止点下重算。

字段与 ``source-fields.json`` 对齐，空值用 ``None``，空集合用 ``[]``。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 基础取值类型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Taint:
    """单个污点来源在某个承载物上的可证明数量区间。

    ``lo`` 是可证明必然属于该缺陷范围的数量（确定受影响下界），
    ``hi`` 是最坏情况下可能属于该范围的数量（可能受影响上界），
    ``capacity`` 是承载物总容量。对序号承载物三者只取 0/1。

    区间恒满足 ``0 <= lo <= hi <= capacity``。判断分三类：

    * ``lo == hi == 0``：可排除；
    * ``lo == hi == capacity``（且 capacity>0）：确定受影响；
    * 其余：可能受影响，且保留整批联合预算（见 :class:`JointBudget`）。
    """

    lo: int
    hi: int
    capacity: int

    @property
    def category(self) -> str:
        if self.lo == 0 and self.hi == 0:
            return "excluded"
        if self.capacity > 0 and self.lo == self.hi == self.capacity:
            return "confirmed"
        return "possible"

    @property
    def is_clear(self) -> bool:
        return self.hi == 0

    @property
    def is_blocking(self) -> bool:
        """是否构成发货/放行限制（确定或可能都限制）。"""
        return self.hi > 0


@dataclass(frozen=True)
class JointBudget:
    """一组共享同一匿名箱的分支共同占用的整批数量界限。

    各分支的最坏值不允许直接相加：真正的联合上界由匿名箱中缺陷件总数
    ``total_bad`` 约束。``branch_hi`` 给出各分支单独上界，``total_bad``
    是跨分支求和后不得超过的整批预算。
    """

    pool_id: str
    notice_id: str
    total_bad: int
    branch_hi: dict[str, int] = field(default_factory=dict)

    @property
    def joint_hi(self) -> int:
        return min(self.total_bad, sum(self.branch_hi.values()))


# ---------------------------------------------------------------------------
# 来源事实记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PartRecord:
    """进料记录（parts）：一个带序号的工艺时刻组件。"""

    id: str
    plant: str
    lot: str
    item: str
    proc: int
    serial: str

    @property
    def scope_key(self) -> tuple[str, str, str]:
        """跨系统编号的有效段：厂别 + 物料 + 批号。"""
        return (self.plant, self.item, self.lot)


@dataclass(frozen=True)
class PoolRecord:
    """同箱供料候选组（pool）。

    ``members`` 是来源已登记的候选序号；``unit_qty`` 是拆装前实际数量。
    对只有数量、未知序号的整箱投入，``members`` 可为空，匿名量由
    ``unit_qty`` 表达，追溯只输出数量界限而不补造身份。
    """

    id: str
    batch: str
    members: tuple[str, ...]
    unit_qty: int


@dataclass(frozen=True)
class AssemblyRecord:
    """总装或拆分去向（assemblies）。

    ``source`` 要么是精确零件序号，要么是候选组 id；``group`` 下同一
    组件只能被使用一次，累计 ``qty`` 不得超过候选组 ``unit_qty``。
    """

    id: str
    unit: str
    source: str
    qty: int
    at: int
    line: str
    intake: int


@dataclass(frozen=True)
class AssignmentRecord:
    """箱内投入对应序号的独立证据（assignments）。"""

    id: str
    assembly: str
    part: str
    intake: int


@dataclass(frozen=True)
class ReworkRecord:
    """返工（rework）：更换命名配置中的构件并登记拆下件去向。"""

    id: str
    unit: str
    old_configuration: str
    new_configuration: str
    removed: str
    replacement: str
    destination: str
    at: int
    intake: int


@dataclass(frozen=True)
class CertificateRecord:
    """维修放行/复检凭证（certificates）。

    仅当同时覆盖指定 defect、unit 与 unit 当前 configuration，且
    ``scope`` 为缺陷放行类时，才在 ``issued_min`` 起解除对应限制。
    """

    id: str
    unit: str
    configuration: str
    defect: str
    issued_min: int
    scope: str
    intake: int


@dataclass(frozen=True)
class NoticeRecord:
    """供应商缺陷通告（notices）。

    ``span`` 为半开工艺时刻区间 ``[lo, hi)``；``replaces`` 指向被本通告
    明确取代的旧通告。编号解释严格限定在 ``(plant, item, lot)`` 段内。
    """

    id: str
    defect: str
    plant: str
    lot: str
    item: str
    span: tuple[int, int]
    replaces: str | None
    intake: int

    @property
    def scope_key(self) -> tuple[str, str, str]:
        return (self.plant, self.item, self.lot)

    def covers_proc(self, proc: int) -> bool:
        return self.span[0] <= proc < self.span[1]


@dataclass(frozen=True)
class ShipmentRecord:
    """发货（shipments）。``received`` 为 None 表示在途。"""

    id: str
    customer: str
    units: tuple[str, ...]
    at: int
    received: int | None
    intake: int


@dataclass(frozen=True)
class ReplyRecord:
    """收货方回复（replies）：只对属于该客户的交付单位有效。"""

    id: str
    shipment: str
    customer: str
    units: tuple[str, ...]
    kind: str
    intake: int


# ---------------------------------------------------------------------------
# 处置结论（引擎重算产物，不入库）
# ---------------------------------------------------------------------------

# 处置类别取值（质控与发货台共用同一套词汇）：
#   hold          在厂限制（确定/可能受影响，等待核对或报废）
#   released      在厂可放行（暴露可排除，或有效凭证覆盖当前配置）
#   in_transit    在途：暂缓待办，库存不能再解锁
#   returned_hold 客户退回后仍在厂限制
#   returned_free 客户退回且当前依据下可放行
#   scrapped      已按报废处理（本样例由处置动作驱动，预留）
DISPOSITION_STATES = frozenset(
    {"hold", "released", "in_transit", "returned_hold", "returned_free", "scrapped"}
)

# 发货台动作：
#   allow        可发货
#   hold_order   订单须暂缓（最坏情况下可能进入）
#   notify_only  已离厂，只能发通知与回复待办，不能按库存解锁
SHIP_ACTIONS = frozenset({"allow", "hold_order", "notify_only"})


@dataclass(frozen=True)
class UnitExposure:
    """一个整机/装配件在某通告版本下的暴露结论。"""

    unit: str
    taint: Taint
    configuration: str | None
    location: str          # plant / in_transit / returned / destination 库存位
    certificate_id: str | None
    disposition: str
    ship_action: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PartExposure:
    """一个零件（含返工拆下旧件）在某通告版本下的暴露结论。"""

    part_id: str
    serial: str
    location: str          # consumed / hold-bin 等库存去向
    taint: Taint
    disposition: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ShipmentExposure:
    """一张交付单在某通告版本下的通知与回复待办。"""

    shipment_id: str
    customer: str
    notified_units: tuple[str, ...]
    open_units: tuple[str, ...]          # 尚未收到该客户有效回复
    replied: tuple[tuple[str, str, str], ...]  # (reply_id, unit, kind)
    basis_changed: bool                  # 依据已被修正改变，但历史不删
    action: str


@dataclass(frozen=True)
class Evaluation:
    """一次完整重算的结果。"""

    notice_id: str
    cutoff_intake: int
    units: dict[str, UnitExposure]
    parts: dict[str, PartExposure]
    shipments: dict[str, ShipmentExposure]
    joint_budgets: tuple[JointBudget, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
