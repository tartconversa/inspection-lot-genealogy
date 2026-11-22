# 装配批次的检验凭证与停售追溯仓

装配线缺陷追查和发货处置的输入合同。数量以单件为单位。 资料为本地合成样本。

## 源交付

`sources/` 存放 JSON 原件，文件名前两位是到达序号，同一序号内的各表属于同次交付。每个文件有 `intake`、`table`、`records`，记录 `id` 在所属表内唯一。`source-fields.json` 列出各表字段类型，空值用 `null`，空集合用 `[]`。业务时间按下方约定解释。

| 数据表 | 说明 |
| --- | --- |
| `parts` | 进料及工艺时刻。 |
| `pool` | 同箱供料的候选组成。 |
| `assemblies` | 总装或拆分去向。source 为投入批组或精确序号。 |
| `assignments` | 确认箱内投入对应序号的独立证据。 |
| `rework` | 更换了命名配置中的部分构件并记录拆下件。 |
| `certificates` | 维修放行签发凭证。 |
| `notices` | 供应商缺陷适用的材料工艺范围。 |
| `shipments` | 发货、签收及客户对象，at 为发货时间，received 为签收时间。 |
| `replies` | 收货方针对交付中的部分处理。 |

## 来源约定

物料批号只可在 plant、item、span 范围解释。装配动作 input 可以指具体序号或候选组；group 下同一组件只能使用一次，kit 收料 unit_qty 是拆装前的实际数量，拆装输入和去向共同约束暴露。厂内序号在同一个对象生存期内稳定。外部出货按客户逐条签收，未签收处于在途。
notice 的 span 以组件实际工艺时刻为准。修订内容明确取代 replaces。更换记录的 removed 必须留去向，replacement 可以来自已证明独立的批次。如果验收证书覆盖特定 defect、unit 及 configuration，则对该配置从 issued_min 生效。未知投入仍保留数量界限，调查员后来提交的 assignment 为能够确认该组实际使用关系的凭证。收货方回复只有 shipment 中属于该客户的 unit 才有效。

## 资料盘点

运行环境为 Python 3.11，盘点与资料校验仅使用标准库。盘点结果按到达批次显示表名及行数。

```sh
python source_inventory.py
python -m unittest discover -s tests -v
```

## 追溯仓（traceware）

在来源合同之上实现的本地追溯仓，用进料、装配、移库、拆件、复检与发货确认记录
计算可核实的暴露范围，并给质控与发货台提供一致的处理状态。仅使用标准库。

| 模块 | 职责 |
| --- | --- |
| `traceware/model.py` | 事实记录与处置结论的数据模型；`Taint` 表达 `[lo,hi]` 数量界限。 |
| `traceware/store.py` | JSONL 只追加事件仓，原子提交、事件 id 幂等、可安全重放。 |
| `traceware/loader.py` | 来源结构校验、字段类型检查与事件规范化；不合法交付入库前拒绝。 |
| `traceware/engine.py` | 建图校验、污点区间传播、通告修订链、凭证适用性、发货通知与待办。 |
| `traceware/service.py` | 从事件仓重建引擎，对外提供装载/评估/查询的统一操作。 |
| `traceware/cli.py` | 质控与发货台共用命令行。 |

### 追溯规则与来源约定的对应

* **有效段编号**：序号只在 `(plant, item, lot)` 内解释；同序号跨段直接报错，
  跨段同批号（如 north/L7 与 south/L7）互不解释。
* **三类暴露边界**：每个承载物输出 `Taint(lo, hi, capacity)`——`lo==hi==0`
  可排除、`lo==hi==capacity` 确定受影响、其余为可能受影响。只有数量、未知
  序号的整箱投入不补造身份。
* **整批联合预算**：匿名分支按组合可行性独立求界限，与抽取顺序无关（顺序不是
  身份证据，串行扣预算等价于 FIFO）。`JointBudget.joint_hi` 以整批缺陷件总数
  约束各分支，分支最坏值不直接相加。后补 assignment 可分批缩小范围。
* **返工与凭证**：拆下旧件保留 `destination` 独立库存去向；返工后的整机即使
  换上已证明独立的好件，在凭证同时覆盖 defect、unit 与当前 configuration 且
  scope 为 `defect-clearance` 前限制不解除。`inspected`（治具复检）与旧配置
  凭证只留证、不放行。
* **发货台**：在厂整机按暴露给 `allow/hold_order`；已离厂货不能按库存解锁，
  生成按收货确认范围的通知与回复待办（`notify_only`）。回复只对属于该客户
  交付单的单位有效；部分回收不抵销其它客户未回复的货。
* **通告修正**：`replaces` 形成修订链，重算只改当前结论；`history()` 永久保留
  每个旧版本的暴露快照、已完成复检与客户回收。`basis_changed` 同时捕捉整机
  界限变化与其依赖候选组的整批预算收窄。
* **可继续处理**：事件仓只追加且幂等，一次接收只到一半时已提交表保留、未提交
  表下次续传；任何时刻可从仓完整重建出一致状态。

### 命令行

隔离模式（`-I`）不会把脚本目录加入 `sys.path`，根目录运行器已按 `__file__`
显式注入项目根，因此下列命令在 `-I` 下可直接运行，且不会从来源资料目录加载
同名模块：

```sh
python -I run_traceware.py load            # 校验并装载全部来源
python -I run_traceware.py load --intake 2 # 只装载到指定到达批次（部分接收）
python -I run_traceware.py status          # 当前暴露、处置、联合预算与待办
python -I run_traceware.py lot north sensor L7   # 按料批查询历次处置
python -I run_traceware.py shipment ship-c      # 按交付单查询与历史快照
python -I run_traceware.py history         # 通告修订链与各版本历史结论
python -I run_traceware.py replay          # 重建仓并校验重放结果一致
python -I run_traceware.py reset           # 清空本地事件仓
python -I run_tests.py                     # 运行全部追溯仓测试（含来源契约）
```

事件仓默认落在仓库内 `.traceware/`（已被 `.gitignore` 忽略），可用 `--store`
指定其它目录。

