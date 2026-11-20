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
