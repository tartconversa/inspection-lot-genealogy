"""追溯引擎：建图、污点区间传播、处置判定与通知待办。

设计要点（对应来源约定）：

* 编号只在 ``(plant, item, lot)`` 段内解释；序号解析要求全局唯一，
  同序号跨段冲突直接报错而不是猜测。
* 匿名箱（只有数量、未知序号）传播的是数量区间 ``[lo, hi]``：
  ``lo`` 可证明必然受影响，``hi`` 最坏情况下可能受影响，``hi==0``
  才可排除。沿多分支转运时输出整批联合预算，分支最坏值不相加、
  不使用先进先出补造身份。
* 返工拆下件保留独立库存去向；整机能否解除限制同时取决于当前暴露
  与“覆盖 defect + unit + 当前 configuration”的有效放行凭证。
* 通告 ``replaces`` 形成修订链；重算只改当前结论，历史通知、已完成
  复检与客户已执行的回收全部保留可查。

引擎本身无突变状态：每次 :meth:`TraceEngine.evaluate` 都在局部字典中
完成污点传播，重复评估与历史快照互不污染。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import (
    AssemblyRecord,
    AssignmentRecord,
    CertificateRecord,
    Evaluation,
    JointBudget,
    NoticeRecord,
    PartExposure,
    PartRecord,
    PoolRecord,
    ReplyRecord,
    ReworkRecord,
    ShipmentExposure,
    ShipmentRecord,
    Taint,
    UnitExposure,
)

# scope 字段中只有缺陷放行类凭证可以解除限制；inspected 表示同编号治具
# 复检等检验记录，只能作为证据留存，不解除限制。
CLEARANCE_SCOPES = frozenset({"defect-clearance"})


class TraceBuildError(ValueError):
    """跨表引用或数量约束不满足，无法建立可核实的追溯图。"""


# ---------------------------------------------------------------------------
# 内部流图结构
# ---------------------------------------------------------------------------


@dataclass
class _FlowEdge:
    """一次数量为 qty 的流转：source 承载物 -> unit。"""

    assembly_id: str
    unit: str
    source: str
    qty: int
    at: int
    line: str
    assigned_part: str | None = None  # 建图时按全量证据标注，评估时按截止点过滤


@dataclass
class _PoolState:
    record: PoolRecord
    edges: list[_FlowEdge] = field(default_factory=list)


@dataclass
class _UnitState:
    unit: str
    build_edges: list[_FlowEdge] = field(default_factory=list)
    reworks: list[ReworkRecord] = field(default_factory=list)
    certificates: list[CertificateRecord] = field(default_factory=list)
    shipment: ShipmentRecord | None = None
    returned: list[ReplyRecord] = field(default_factory=list)
    configuration: str | None = None


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------


class TraceEngine:
    def __init__(self, records_by_table: dict[str, list]):
        self.raw = records_by_table
        self.warnings: list[str] = []
        self._build()

    # ------------------------------------------------------------------
    # 建图与引用一致性校验
    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.parts: dict[str, PartRecord] = {p.id: p for p in self.raw.get("parts", [])}
        self.pools: dict[str, PoolRecord] = {g.id: g for g in self.raw.get("pool", [])}
        self.assemblies: dict[str, AssemblyRecord] = {
            a.id: a for a in self.raw.get("assemblies", [])
        }
        self.assignments: list[AssignmentRecord] = self.raw.get("assignments", [])
        self.reworks: list[ReworkRecord] = sorted(
            self.raw.get("rework", []), key=lambda r: (r.at, r.id)
        )
        self.certificates: list[CertificateRecord] = sorted(
            self.raw.get("certificates", []), key=lambda c: (c.issued_min, c.id)
        )
        self.notices: list[NoticeRecord] = self.raw.get("notices", [])
        self.shipments: dict[str, ShipmentRecord] = {
            s.id: s for s in self.raw.get("shipments", [])
        }
        self.replies: list[ReplyRecord] = self.raw.get("replies", [])

        # 序号索引：厂内序号在同一对象生存期内稳定；同序号跨段冲突拒绝猜测。
        self.serial_index: dict[str, PartRecord] = {}
        for part in self.parts.values():
            existing = self.serial_index.get(part.serial)
            if existing is not None and existing.scope_key != part.scope_key:
                raise TraceBuildError(
                    f"序号 {part.serial} 同时出现在不同有效段 "
                    f"{existing.scope_key} 与 {part.scope_key}，拒绝解释"
                )
            self.serial_index[part.serial] = part

        self._build_units_and_edges()
        self._validate_pools_and_assignments()
        self._validate_reworks()
        self._validate_shipments_and_replies()

    def _build_units_and_edges(self) -> None:
        self.units: dict[str, _UnitState] = {}
        self.pool_states: dict[str, _PoolState] = {
            gid: _PoolState(record=g) for gid, g in self.pools.items()
        }

        def get_unit(unit: str) -> _UnitState:
            if unit not in self.units:
                self.units[unit] = _UnitState(unit=unit)
            return self.units[unit]

        for asm in sorted(self.assemblies.values(), key=lambda a: (a.at, a.id)):
            if asm.qty <= 0:
                raise TraceBuildError(f"装配 {asm.id} 数量必须为正")
            if asm.source not in self.pools and asm.source not in self.serial_index:
                raise TraceBuildError(
                    f"装配 {asm.id} 的 source {asm.source!r} 既不是候选组也不是已知序号"
                )
            edge = _FlowEdge(
                assembly_id=asm.id, unit=asm.unit, source=asm.source,
                qty=asm.qty, at=asm.at, line=asm.line,
            )
            get_unit(asm.unit).build_edges.append(edge)
            if asm.source in self.pool_states:
                self.pool_states[asm.source].edges.append(edge)

        self.assignment_by_asm: dict[str, AssignmentRecord] = {}
        for evidence in self.assignments:
            if evidence.assembly not in self.assemblies:
                raise TraceBuildError(
                    f"assignment {evidence.id} 指向未知装配 {evidence.assembly}"
                )
            if evidence.assembly in self.assignment_by_asm:
                raise TraceBuildError(
                    f"装配 {evidence.assembly} 存在多条 assignment，证据冲突"
                )
            self.assignment_by_asm[evidence.assembly] = evidence
            asm = self.assemblies[evidence.assembly]
            for edge in self.units[asm.unit].build_edges:
                if edge.assembly_id == evidence.assembly:
                    edge.assigned_part = evidence.part

        for cert in self.certificates:
            if cert.unit not in self.units:
                raise TraceBuildError(f"凭证 {cert.id} 指向未知整机 {cert.unit}")
            self.units[cert.unit].certificates.append(cert)

        for rw in self.reworks:
            if rw.unit not in self.units:
                raise TraceBuildError(f"返工 {rw.id} 指向未知整机 {rw.unit}")
            self.units[rw.unit].reworks.append(rw)

        for shipment in self.shipments.values():
            for unit in shipment.units:
                if unit not in self.units:
                    raise TraceBuildError(
                        f"发货 {shipment.id} 包含未知整机 {unit}"
                    )
                state = self.units[unit]
                if state.shipment is not None:
                    raise TraceBuildError(
                        f"整机 {unit} 同时出现在发货 {state.shipment.id} 与 "
                        f"{shipment.id}，去向不唯一"
                    )
                state.shipment = shipment

        # 回复按整机接线（有效性在评估时再按客户/归属过滤）。
        for reply in self.replies:
            shipment = self.shipments.get(reply.shipment)
            if shipment is None:
                raise TraceBuildError(
                    f"回复 {reply.id} 指向未知发货 {reply.shipment}"
                )
            for unit in reply.units:
                if unit in shipment.units and reply.customer == shipment.customer:
                    self.units[unit].returned.append(reply)

    def _validate_pools_and_assignments(self) -> None:
        for gid, state in self.pool_states.items():
            pool = state.record
            if pool.unit_qty <= 0:
                raise TraceBuildError(f"候选组 {gid} unit_qty 必须为正")
            unknown = [s for s in pool.members if s not in self.serial_index]
            if unknown:
                raise TraceBuildError(f"候选组 {gid} 含未知序号 {unknown}")
            if len(set(pool.members)) != len(pool.members):
                raise TraceBuildError(f"候选组 {gid} 成员序号重复")
            if len(pool.members) > pool.unit_qty:
                raise TraceBuildError(
                    f"候选组 {gid} 登记成员 {len(pool.members)} 超过实际数量 "
                    f"{pool.unit_qty}"
                )
            total_drawn = sum(edge.qty for edge in state.edges)
            if total_drawn > pool.unit_qty:
                raise TraceBuildError(
                    f"候选组 {gid} 累计投入 {total_drawn} 超过箱内数量 "
                    f"{pool.unit_qty}，数量约束被破坏"
                )
            assigned_parts: set[str] = set()
            for edge in state.edges:
                evidence = self.assignment_by_asm.get(edge.assembly_id)
                if evidence is None:
                    continue
                if evidence.part not in pool.members:
                    raise TraceBuildError(
                        f"assignment {evidence.id} 的零件 {evidence.part} 不属于"
                        f"候选组 {gid}"
                    )
                if evidence.part in assigned_parts:
                    raise TraceBuildError(
                        f"零件 {evidence.part} 被 assignment 重复指派，"
                        "同一组件不能在 group 下使用多次"
                    )
                assigned_parts.add(evidence.part)

    def _validate_reworks(self) -> None:
        for rw in self.reworks:
            for serial in (rw.removed, rw.replacement):
                if serial not in self.serial_index:
                    raise TraceBuildError(f"返工 {rw.id} 引用未知序号 {serial}")
            if not rw.destination:
                raise TraceBuildError(
                    f"返工 {rw.id} 的 removed 必须留去向（destination 为空）"
                )

    def _validate_shipments_and_replies(self) -> None:
        for reply in self.replies:
            shipment = self.shipments.get(reply.shipment)
            if shipment is None:
                continue
            if reply.customer != shipment.customer:
                self.warnings.append(
                    f"回复 {reply.id} 客户 {reply.customer} 与发货客户 "
                    f"{shipment.customer} 不符，整条回复无效"
                )
                continue
            foreign = [u for u in reply.units if u not in shipment.units]
            if foreign:
                self.warnings.append(
                    f"回复 {reply.id} 含不属于该交付的整机 {foreign}，"
                    "这些单位不计入回收"
                )

    # ------------------------------------------------------------------
    # 续证入口：操作人员后补 assignment 证据时同步边标注。
    # 持久化路径仍走事件仓（store/loader 在 _build 统一接线）；本方法用于
    # 内存态追加证据并立即重算，重复指派或越组证据会被拒绝。
    # ------------------------------------------------------------------
    def register_assignment(self, evidence: AssignmentRecord) -> None:
        if evidence.assembly in self.assignment_by_asm:
            raise TraceBuildError(
                f"装配 {evidence.assembly} 存在多条 assignment，证据冲突"
            )
        asm = self.assemblies[evidence.assembly]
        pool_state = self.pool_states.get(asm.source)
        if pool_state is not None:
            pool = pool_state.record
            if evidence.part not in pool.members:
                raise TraceBuildError(
                    f"assignment {evidence.id} 的零件 {evidence.part} 不属于"
                    f"候选组 {asm.source}"
                )
        self.assignments.append(evidence)
        self.assignment_by_asm[evidence.assembly] = evidence
        for edge in self.units[asm.unit].build_edges:
            if edge.assembly_id == evidence.assembly:
                edge.assigned_part = evidence.part

    # ------------------------------------------------------------------
    # 通告修订链
    # ------------------------------------------------------------------
    def notice_chains(self) -> dict[str, list[NoticeRecord]]:
        """按缺陷+有效段分组，沿 replaces 还原修订链（旧 -> 新）。"""
        groups: dict[tuple[str, tuple[str, str, str]], list[NoticeRecord]] = {}
        for notice in self.notices:
            groups.setdefault((notice.defect, notice.scope_key), []).append(notice)

        chains: dict[str, list[NoticeRecord]] = {}
        for (defect, scope), group_notices in groups.items():
            by_id = {n.id: n for n in group_notices}
            replaced_ids = {n.replaces for n in group_notices if n.replaces}
            for target in replaced_ids:
                if target not in by_id:
                    self.warnings.append(
                        f"通告 replaces={target} 在本资料中缺失，链不完整"
                    )
            heads = [n for n in group_notices if n.id not in replaced_ids]
            if len(heads) != 1:
                self.warnings.append(
                    f"缺陷 {defect} 段 {scope} 通告链头不唯一 {[h.id for h in heads]}"
                )
            head = sorted(heads, key=lambda n: n.intake)[-1] if heads else None
            ordered: list[NoticeRecord] = []
            current = head
            seen: set[str] = set()
            while current is not None and current.id not in seen:
                seen.add(current.id)
                ordered.append(current)
                nxt_id = current.replaces
                current = by_id.get(nxt_id) if nxt_id else None
            leftover = [n for n in group_notices if n.id not in seen]
            ordered.extend(sorted(leftover, key=lambda n: n.intake))
            chain = list(reversed(ordered))  # 旧 -> 新
            for notice in chain:
                chains[notice.id] = chain
        return chains

    def effective_notice(self, cutoff_intake: int) -> NoticeRecord | None:
        """截止点下最新有效通告。

        沿修订链取“链上已到达的最新版本”：链头若尚未到达，回退到链上
        最后一个 intake 不超过截止点的版本，而不是整体判定为无通告。
        """
        chains = self.notice_chains()
        heads: list[NoticeRecord] = []
        for notice in self.notices:
            if notice.intake > cutoff_intake:
                continue
            chain = chains.get(notice.id, [notice])
            arrived = [n for n in chain if n.intake <= cutoff_intake]
            if arrived:
                latest = max(arrived, key=lambda n: n.intake)
                if latest not in heads:
                    heads.append(latest)
        if not heads:
            return None
        return sorted(heads, key=lambda n: (n.intake, n.id))[-1]

    # ------------------------------------------------------------------
    # 污点传播（单次评估内的纯函数计算）
    # ------------------------------------------------------------------
    def _part_is_bad(self, part: PartRecord, notice: NoticeRecord) -> bool:
        return part.scope_key == notice.scope_key and notice.covers_proc(part.proc)

    def _pool_scope(self, pool: PoolRecord) -> tuple[str, str, str] | None:
        """推断候选组所属有效段。

        成员序号全部落在同一 ``(plant, item, lot)`` 时，匿名量与成员同段；
        成员为空或跨段时无法确定作用域，匿名量不与任何通告段匹配。
        """
        scopes = {
            self.serial_index[serial].scope_key
            for serial in pool.members
            if serial in self.serial_index
        }
        return next(iter(scopes)) if len(scopes) == 1 else None

    def _pool_edge_taints(
        self,
        state: _PoolState,
        notice: NoticeRecord,
        visible_assignments: set[str],
    ) -> tuple[dict[str, Taint], int, int]:
        """返回 (assembly_id -> 边污点, 缺陷件下界, 缺陷件上界)。

        匿名分支的可证明界限按组合可行性计算，与装配（抽取）顺序无关——
        顺序本身不是身份证据，按顺序串行扣预算会变相做 FIFO 补身份：

        * 分支上界：该分支容量、整批缺陷件上界、匿名容量三者取小；
        * 分支下界：仅当“把该分支完全排除后，其余匿名容量装不下全部
          缺陷件”时才可证明必然命中，即
          ``lo = max(0, 缺陷件下界 - 箱内其它匿名容量)``；
        * assignment 已确认身份的分支按确定结果 0/1 计算，并从匿名容量
          与缺陷件预算中整体扣除（好件坏件都占容量）。
        """
        pool = state.record
        confirmed_bad = sum(
            1 for s in pool.members if self._part_is_bad(self.serial_index[s], notice)
        )
        anonymous = pool.unit_qty - len(pool.members)
        # 匿名量与候选组同段：通告覆盖该段时匿名件全部进入“可能受影响”
        # 集合（无序号，不补身份）；否则匿名量不命中该通告。
        scope = self._pool_scope(pool)
        anonymous_bad = anonymous if scope == notice.scope_key else 0
        bad_lo, bad_hi = confirmed_bad, confirmed_bad + anonymous_bad

        taints: dict[str, Taint] = {}
        # 已由 assignment 确认身份的分支，其消耗从箱内锁定容量。
        locked_bad = locked_good = 0
        for edge in state.edges:
            evidence = self.assignment_by_asm.get(edge.assembly_id)
            if evidence is None or evidence.assembly not in visible_assignments:
                continue
            part = self.serial_index[evidence.part]
            bad = 1 if self._part_is_bad(part, notice) else 0
            taints[edge.assembly_id] = Taint(bad, bad, edge.qty)
            if bad:
                locked_bad += edge.qty
            else:
                locked_good += edge.qty

        # 匿名分支可触及的容量 = 箱总量 - 已确认分支锁定的容量。
        # 其中包含未被任何分支消耗的登记成员（它们对匿名分支而言同样
        # 是候选），以及无序号的纯匿名位。
        anonymous_capacity = pool.unit_qty - locked_bad - locked_good
        anon_bad_lo = max(0, bad_lo - locked_bad)
        anon_bad_hi = max(0, bad_hi - locked_bad)
        anon_bad_hi = min(anon_bad_hi, anonymous_capacity)
        for edge in state.edges:
            if edge.assembly_id in taints:
                continue
            other_capacity = anonymous_capacity - edge.qty
            # 最坏情况：缺陷件尽量多进入本分支。
            hi = min(edge.qty, anon_bad_hi, anonymous_capacity)
            # 最好情况：缺陷件尽量避开本分支；装不下的差额必然落入本分支。
            lo = max(0, anon_bad_lo - other_capacity)
            lo = min(lo, hi)
            taints[edge.assembly_id] = Taint(lo, hi, edge.qty)
        return taints, bad_lo, bad_hi

    def _unit_taint(
        self,
        unit_state: _UnitState,
        notice: NoticeRecord,
        cutoff: int,
        edge_taints: dict[str, Taint],
    ) -> Taint:
        lo = hi = cap = 0
        for edge in unit_state.build_edges:
            if edge.assembly_id in edge_taints:
                taint = edge_taints[edge.assembly_id]
            else:
                part = self.serial_index[edge.source]  # 直投精确序号
                bad = 1 if self._part_is_bad(part, notice) else 0
                taint = Taint(bad, bad, edge.qty)
            lo += taint.lo
            hi += taint.hi
            cap += taint.capacity
        # 返工：拆下件带走其污点，替换件按自身批次污点进入。
        for rw in unit_state.reworks:
            if rw.intake > cutoff:
                continue
            r_bad = 1 if self._part_is_bad(self.serial_index[rw.removed], notice) else 0
            p_bad = (
                1 if self._part_is_bad(self.serial_index[rw.replacement], notice) else 0
            )
            lo += p_bad - r_bad
            hi += p_bad - r_bad
        lo = max(0, lo)
        hi = max(lo, hi)
        return Taint(lo, min(hi, cap), cap)

    # ------------------------------------------------------------------
    def evaluate(
        self,
        cutoff_intake: int | None = None,
        *,
        notice_id: str | None = None,
    ) -> Evaluation | None:
        """重算截止点下的暴露与处置。

        ``cutoff_intake=None`` 使用全部已装载证据；``notice_id`` 用于历史
        快照，显式按某一通告版本（及其修订链上下文）重算，而不是取链头。
        """
        cutoff = self.max_intake() if cutoff_intake is None else cutoff_intake
        if notice_id is not None:
            notice = next((n for n in self.notices if n.id == notice_id), None)
            if notice is None or notice.intake > cutoff:
                return None
        else:
            notice = self.effective_notice(cutoff)
        if notice is None:
            return None

        visible_assignments = {
            a.assembly for a in self.assignments if a.intake <= cutoff
        }
        # 池 -> 边污点，全部局限在本评估的局部字典。
        edge_taints: dict[str, Taint] = {}
        pool_bounds: dict[str, tuple[int, int]] = {}
        for gid, state in self.pool_states.items():
            taints, bad_lo, bad_hi = self._pool_edge_taints(
                state, notice, visible_assignments
            )
            edge_taints.update(taints)
            pool_bounds[gid] = (bad_lo, bad_hi)

        unit_exposures: dict[str, UnitExposure] = {}
        unit_taints: dict[str, Taint] = {}
        for unit_id, state in self.units.items():
            state.configuration = (
                state.reworks[-1].new_configuration if state.reworks else None
            )
            taint = self._unit_taint(state, notice, cutoff, edge_taints)
            unit_taints[unit_id] = taint
            cert_id, cert_reasons = self._applicable_certificate(state, notice, cutoff)
            disposition, location, reasons, action = self._disposition(
                state, taint, cert_id, cert_reasons, cutoff
            )
            unit_exposures[unit_id] = UnitExposure(
                unit=unit_id, taint=taint, configuration=state.configuration,
                location=location, certificate_id=cert_id,
                disposition=disposition, ship_action=action,
                reasons=tuple(reasons),
            )

        part_locations = self._part_locations(cutoff)
        part_exposures = self._part_exposures(notice, part_locations)
        shipment_exposures = self._shipment_exposures(
            notice, cutoff, unit_taints
        )
        joint_budgets = tuple(
            JointBudget(
                pool_id=gid,
                notice_id=notice.id,
                total_bad=pool_bounds[gid][1],
                # 收集全部分支（含上界为 0 的已排除分支），便于发货台核对；
                # 联合上界由 total_bad 约束，不能把 branch_hi 直接相加。
                branch_hi={
                    edge.unit: edge_taints[edge.assembly_id].hi
                    for edge in state.edges
                },
            )
            for gid, state in self.pool_states.items()
        )
        return Evaluation(
            notice_id=notice.id,
            cutoff_intake=cutoff,
            units=unit_exposures,
            parts=part_exposures,
            shipments=shipment_exposures,
            joint_budgets=joint_budgets,
            warnings=tuple(self.warnings),
        )

    # ------------------------------------------------------------------
    def _part_locations(self, cutoff: int) -> dict[str, str]:
        """每个零件当前去向：consumed（装在某整机上）或独立库存位。

        位置按零件记录 id 维护（与 PartExposure 的键一致）。
        """
        locations: dict[str, str] = {}
        for asm in self.assemblies.values():
            if asm.intake > cutoff:
                continue
            if asm.source in self.serial_index:
                locations[self.serial_index[asm.source].id] = "consumed"
        for evidence in self.assignments:
            if evidence.intake <= cutoff:
                locations[self.serial_index[evidence.part].id] = "consumed"
        for rw in self.reworks:
            if rw.intake > cutoff:
                continue
            # 旧件仍有独立库存去向；替换件转为装机。
            locations[self.serial_index[rw.removed].id] = rw.destination
            locations[self.serial_index[rw.replacement].id] = "consumed"
        return locations

    def _applicable_certificate(
        self, state: _UnitState, notice: NoticeRecord, cutoff: int
    ) -> tuple[str | None, list[str]]:
        reasons: list[str] = []
        expected_config = state.configuration
        for cert in state.certificates:
            if cert.intake > cutoff:
                continue
            if cert.defect != notice.defect:
                reasons.append(
                    f"凭证 {cert.id} 覆盖缺陷 {cert.defect}，与 {notice.defect} 不符"
                )
                continue
            if expected_config is not None and cert.configuration != expected_config:
                reasons.append(
                    f"凭证 {cert.id} 配置 {cert.configuration} 不覆盖当前配置 "
                    f"{expected_config}"
                )
                continue
            if cert.scope not in CLEARANCE_SCOPES:
                reasons.append(
                    f"凭证 {cert.id} scope={cert.scope} 仅为复检记录，不解除限制"
                )
                continue
            return cert.id, reasons
        return None, reasons

    def _disposition(
        self,
        state: _UnitState,
        taint: Taint,
        cert_id: str | None,
        cert_reasons: list[str],
        cutoff: int,
    ) -> tuple[str, str, list[str], str]:
        reasons = list(cert_reasons)
        shipment = state.shipment
        # 是否离厂取决于发货记录在证据截止点是否已到达，而不是业务时刻。
        shipped = shipment is not None and shipment.intake <= cutoff
        latest_return = next(
            (
                r for r in reversed(sorted(state.returned, key=lambda r: r.intake))
                if r.intake <= cutoff
            ),
            None,
        )

        if taint.category == "confirmed":
            reasons.append("当前配置含确定受影响组件")
        elif taint.category == "possible":
            reasons.append(
                f"匿名投入数量界限 [{taint.lo},{taint.hi}]/{taint.capacity}，"
                "最坏情况下可能受影响"
            )

        # 返工后的整机即使换上已证明独立的好件，也必须由覆盖当前配置的
        # 有效放行凭证确认；工单关闭/治具复检不替代该凭证。
        rework_pending = bool(state.reworks) and cert_id is None
        if rework_pending and taint.is_clear:
            reasons.append(
                "返工后更换件虽可排除，但在放行凭证覆盖新配置前限制不解除"
            )

        cleared = cert_id is not None
        if cleared:
            reasons.append(f"放行凭证 {cert_id} 覆盖缺陷、整机与当前配置")
        elif taint.is_blocking and (state.reworks or state.certificates):
            reasons.append("工单关闭/复检不替代缺陷放行，限制继续有效")

        restricted = taint.is_blocking or rework_pending

        if shipped and latest_return is None:
            return (
                "in_transit",
                "in_transit",
                reasons + [f"已随 {shipment.id} 离厂，不能按库存解锁"],
                "notify_only",
            )
        if latest_return is not None:
            location = f"returned:{latest_return.id}"
            if restricted:
                return "returned_hold", location, reasons, "hold_order"
            return (
                "returned_free",
                location,
                reasons + [
                    f"客户回复 {latest_return.id}（{latest_return.kind}）已回收，"
                    "当前依据下可放行"
                ],
                "allow",
            )
        if restricted:
            return "hold", "plant", reasons, "hold_order"
        if not taint.is_blocking:
            reasons.append("暴露上界为 0，可排除")
        return "released", "plant", reasons, "allow"

    def _part_exposures(
        self, notice: NoticeRecord, part_locations: dict[str, str]
    ) -> dict[str, PartExposure]:
        out: dict[str, PartExposure] = {}
        for part_id, part in self.parts.items():
            bad = self._part_is_bad(part, notice)
            taint = Taint(1 if bad else 0, 1 if bad else 0, 1)
            location = part_locations.get(part_id, "stock")
            reasons: list[str] = []
            if location == "consumed":
                disposition = "consumed"
            elif taint.is_blocking:
                disposition = "hold"
                reasons.append(
                    "缺陷范围内零件，独立库存位限制（待报废或核对）"
                )
            else:
                disposition = "released"
                reasons.append("不在缺陷适用范围，可继续使用")
            out[part_id] = PartExposure(
                part_id=part_id, serial=part.serial, location=location,
                taint=taint, disposition=disposition, reasons=tuple(reasons),
            )
        return out

    # ------------------------------------------------------------------
    # 发货通知与回复待办
    # ------------------------------------------------------------------
    def _valid_replies_for(self, shipment_id: str) -> list[ReplyRecord]:
        shipment = self.shipments[shipment_id]
        out: list[ReplyRecord] = []
        for reply in self.replies:
            if (
                reply.shipment == shipment_id
                and reply.customer == shipment.customer
                and any(u in shipment.units for u in reply.units)
            ):
                out.append(reply)
        return sorted(out, key=lambda r: (r.intake, r.id))

    def _shipment_exposures(
        self,
        notice: NoticeRecord,
        cutoff: int,
        current_unit_taints: dict[str, Taint],
    ) -> dict[str, ShipmentExposure]:
        # 历史通告版本 -> 用当前全部证据按旧版本重算的整机污点。
        # 依据改变的比较口径是“同样证据、不同通告版本”，因此证据截止取当前。
        historical_taints: dict[str, dict[str, Taint]] = {}
        historical_pool_bounds: dict[str, dict[str, tuple[int, int]]] = {}
        visible_now = {a.assembly for a in self.assignments if a.intake <= cutoff}
        for old_notice in self.notices:
            if old_notice.intake > cutoff or old_notice.id == notice.id:
                continue
            historical_taints[old_notice.id] = self._all_unit_taints_at(
                old_notice, cutoff
            )
            historical_pool_bounds[old_notice.id] = {
                gid: self._pool_edge_taints(state, old_notice, visible_now)[1:]
                for gid, state in self.pool_states.items()
            }
        current_pool_bounds = {
            gid: self._pool_edge_taints(state, notice, visible_now)[1:]
            for gid, state in self.pool_states.items()
        }

        def _feeding_pools(unit: str) -> set[str]:
            return {
                edge.source
                for edge in self.units[unit].build_edges
                if edge.source in self.pool_states
            }

        out: dict[str, ShipmentExposure] = {}
        for shipment_id, shipment in self.shipments.items():
            if shipment.intake > cutoff:
                continue
            # 发货时点暴露：通告按发货业务时刻 at 解释，证据只能用发货记录
            # 到达时（shipment.intake）已装载的部分。两者取较早者。
            then_cutoff = min(shipment.intake, shipment.at)
            then_notice = self.effective_notice(then_cutoff)
            if then_notice is None:
                # 发货早于任何通告：发货时点无限制，不会产生历史通知。
                continue
            then_taints = self._all_unit_taints_at(
                then_notice, shipment.intake
            )
            notified = []
            ship_time_restricted: dict[str, bool] = {}
            for unit in shipment.units:
                restricted, _taint = self._restricted_at(
                    unit, then_notice, shipment.intake
                )
                ship_time_restricted[unit] = restricted
                if restricted:
                    notified.append(unit)
            replied_rows: list[tuple[str, str, str]] = []
            replied_units: set[str] = set()
            for reply in self._valid_replies_for(shipment_id):
                if reply.intake > cutoff:
                    continue
                for unit in reply.units:
                    if unit in notified and unit in shipment.units:
                        replied_units.add(unit)
                        replied_rows.append((reply.id, unit, reply.kind))
            open_units = [u for u in notified if u not in replied_units]

            basis_changed = False
            for unit in notified:
                now_t = current_unit_taints[unit]
                then_t = then_taints[unit]
                # 当前限制状态：污点受限，或返工后缺覆盖当前配置的有效凭证。
                state = self.units[unit]
                now_restricted = now_t.is_blocking or (
                    bool(state.reworks)
                    and not self._has_clearance_certificate(state, notice, cutoff)
                )
                # 限制状态（含返工待凭证）或污点界限发生变化，都是依据改变。
                if ship_time_restricted[unit] != now_restricted:
                    basis_changed = True
                if now_t.lo != then_t.lo or now_t.hi != then_t.hi:
                    basis_changed = True
                for old_unit_taints in historical_taints.values():
                    old_t = old_unit_taints.get(unit)
                    if old_t is not None and (
                        old_t.lo != now_t.lo or old_t.hi != now_t.hi
                    ):
                        basis_changed = True
                # 整机界限可能不变，但其依赖候选组的整批缺陷件预算已收窄，
                # 同样属于通告依据改变（可证明的暴露范围变了）。
                for pool_id in _feeding_pools(unit):
                    for old_bounds in historical_pool_bounds.values():
                        if old_bounds.get(pool_id) != current_pool_bounds.get(pool_id):
                            basis_changed = True

            out[shipment_id] = ShipmentExposure(
                shipment_id=shipment_id,
                customer=shipment.customer,
                notified_units=tuple(notified),
                open_units=tuple(open_units),
                replied=tuple(replied_rows),
                basis_changed=basis_changed,
                action="notify_only" if open_units else "allow",
            )
        return out

    def _all_unit_taints_at(
        self, notice: NoticeRecord, cutoff: int
    ) -> dict[str, Taint]:
        """按指定通告与证据截止，重算全部整机污点（纯函数，供历史对比）。"""
        visible_assignments = {
            a.assembly for a in self.assignments if a.intake <= cutoff
        }
        edge_taints: dict[str, Taint] = {}
        for state in self.pool_states.values():
            taints, _lo, _hi = self._pool_edge_taints(
                state, notice, visible_assignments
            )
            edge_taints.update(taints)
        return {
            unit_id: self._unit_taint(state, notice, cutoff, edge_taints)
            for unit_id, state in self.units.items()
        }

    def _has_clearance_certificate(
        self, state: _UnitState, notice: NoticeRecord, cutoff: int
    ) -> bool:
        """截止点下是否存在覆盖当前配置的有效缺陷放行凭证。"""
        configuration = state.reworks[-1].new_configuration if state.reworks else None
        for cert in state.certificates:
            if cert.intake > cutoff or cert.defect != notice.defect:
                continue
            if configuration is not None and cert.configuration != configuration:
                continue
            if cert.scope in CLEARANCE_SCOPES:
                return True
        return False

    def _restricted_at(
        self, unit_id: str, notice: NoticeRecord, cutoff: int
    ) -> tuple[bool, Taint]:
        """发货时点的限制判定：污点受限，或返工后缺有效放行凭证。

        只使用 cutoff 前已到达的返工与凭证，还原发货当时质控/发货台视角。
        """
        state = self.units[unit_id]
        taints = self._all_unit_taints_at(notice, cutoff)
        taint = taints[unit_id]
        visible_reworks = [r for r in state.reworks if r.intake <= cutoff]
        if not visible_reworks:
            return taint.is_blocking, taint
        configuration = visible_reworks[-1].new_configuration
        cert_id = None
        for cert in state.certificates:
            if cert.intake > cutoff:
                continue
            if cert.defect != notice.defect:
                continue
            if cert.configuration != configuration:
                continue
            if cert.scope not in CLEARANCE_SCOPES:
                continue
            cert_id = cert.id
            break
        # 返工后即使替换件可排除，凭证未覆盖前仍限制。
        restricted = taint.is_blocking or cert_id is None
        return restricted, taint

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def max_intake(self) -> int:
        """全部事实记录中最大的到达序号（默认评估截止点）。"""
        intakes: list[int] = []
        for rows in (
            self.parts.values(),
            self.pools.values(),
            self.assemblies.values(),
            self.assignments,
            self.reworks,
            self.certificates,
            self.notices,
            self.shipments.values(),
            self.replies,
        ):
            intakes.extend(getattr(row, "intake", 0) for row in rows)
        return max(intakes, default=0)

    def query_lot(self, plant: str, item: str, lot: str) -> dict:
        """按料批查询历次处置与当前可放行范围。"""
        evaluation = self.evaluate()
        related_parts = [
            p for p in self.parts.values() if p.scope_key == (plant, item, lot)
        ]
        return {
            "scope": {"plant": plant, "item": item, "lot": lot},
            "parts": [
                {
                    "part_id": p.id,
                    "serial": p.serial,
                    "proc": p.proc,
                    "exposure": evaluation.parts[p.id],
                }
                for p in related_parts
            ],
            "units": [
                exposure
                for exposure in evaluation.units.values()
                if self._unit_touches_lot(exposure.unit, plant, item, lot)
            ],
            "history": [
                snap
                for snap in self.history()
                if (snap["notice"].plant, snap["notice"].item, snap["notice"].lot)
                == (plant, item, lot)
            ],
        }

    def query_shipment(self, shipment_id: str) -> dict:
        """按一张交付单查询历次处置及可放行范围。"""
        if shipment_id not in self.shipments:
            raise KeyError(shipment_id)
        evaluation = self.evaluate()
        shipment = self.shipments[shipment_id]
        return {
            "shipment": shipment,
            "current": evaluation.shipments.get(shipment_id),
            "units": [
                evaluation.units[u]
                for u in shipment.units
                if u in evaluation.units
            ],
            "history": [
                snap
                for snap in self.history()
                if shipment_id in snap["evaluation"].shipments
            ],
        }

    def _unit_touches_lot(self, unit: str, plant: str, item: str, lot: str) -> bool:
        state = self.units[unit]
        serials: set[str] = set()
        for edge in state.build_edges:
            if edge.assigned_part:
                serials.add(edge.assigned_part)
            elif edge.source in self.serial_index:
                serials.add(edge.source)
            else:
                serials.update(self.pool_states[edge.source].record.members)
        for rw in state.reworks:
            serials.add(rw.removed)
            serials.add(rw.replacement)
        return any(
            (part := self.serial_index.get(serial)) is not None
            and part.scope_key == (plant, item, lot)
            for serial in serials
        )

    def history(self) -> list[dict]:
        """每个通告版本在其到达时点的评估快照（旧依据永久可追）。"""
        snapshots = []
        for notice_record in sorted(self.notices, key=lambda n: n.intake):
            # 显式按该版本重算，避免同 intake 内被链头覆盖。
            evaluation = self.evaluate(
                notice_record.intake, notice_id=notice_record.id
            )
            if evaluation is None:
                continue
            snapshots.append({"notice": notice_record, "evaluation": evaluation})
        return snapshots

    def releasable_range(self) -> dict:
        """当前依据下的可放行范围汇总（质控/发货台共用视图）。"""
        evaluation = self.evaluate()
        if evaluation is None:
            return {"units": [], "parts": [], "shipments": []}
        return {
            "units": [
                u.unit for u in evaluation.units.values() if u.ship_action == "allow"
            ],
            "parts": [
                p.part_id
                for p in evaluation.parts.values()
                if p.disposition == "released"
            ],
            "shipments": [
                s.shipment_id
                for s in evaluation.shipments.values()
                if s.action == "allow"
            ],
        }
