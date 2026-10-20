# NovelHarness

本系统是上游抽取系统：按小说原文建立人物、地点、世界背景、核心事件及故事时间线，产出的 `repository` 供下游任务读取。技术路线是：**全文联合提取与核对 → 文本分块 → 两遍核心事件、关系和局部时间提取 → 结构修复 → 跨块合并与故事时间校验 → repository**。使用 AutoGen；参照 [Shadow-Loom](https://github.com/dwlmt/shadow-loom) 的抽取、结构修复和跨块时间路线，自行实现所需部分。

提取完整性以原文支持的信息是否进入对应字段衡量，不以非空率衡量。汇集全文中同一对象的资料，保留相对年龄、约略时间、时期及转述限定；正文或传记提到的目的、结果、地点、物品状态仍须填写对应字段。已有核对负责补齐有据遗漏；确无依据的字段保持未知，不新增填表事件、检查阶段或非空门槛。`required_results`保存有据的实际结果与直接后果，`original_goal`保存参与者当时目的，二者不能混用。契约中保留的精确起止、时长、节点间隔仍为null，不为减少空值扩展时间研究范围。

## 工作流程

1. `foundation.extract` 一次识别全文人物、别称、地点、物品和背景，同时形成详细人物档案、有向人物关系经历、物品档案、故事主干及 `initial_state`。初始状态对应故事实际起点之前，不能抄用叙述开头或全文结尾的状态。全文带稳定来源标记，供物品和状态引用。`foundation.check` 独立对照原文，提交具体缺漏或错误的增量修订。合法目录、档案草稿和失败记录分别保存；前置资料不完整时仍处理无依赖的后续文本块。
2. Reader 保留原文位置并分块。不同块可以并行；每块依次执行 `reading → first_pass → second_pass → consequences`。前两遍都遵循同一核心事件标准，职责分别偏重行动/经历及言语/关系/遗漏，不能省略其一。模型引用片段编号，程序回填原文；第二遍对同一发生的事实补充作为检查建议保存，不直接覆盖旧事实，也不另建重复事件；同编号且发生身份未变的局部时间位置可修正并校验。无法确定同一性时仍保留新事件供核对。
3. `consequences` 在既有后果提取中为每个事件提交关系和物品的 `state_delta`，只写变化字段；无变化也明确交空集合。程序检查 JSON、字段、编号、原文引用、事件图与时间结构，并给出精确修复反馈。确定性格式错误有限重交；语义疑点保留给有原文上下文的检查。失败块不阻止无依赖的其他块，候选和有效进展保存在 SQLite。
4. `global.timeline.*` 用各块局部时间草稿和全文，将现存事件归入按**故事实际开始时间**排列的节点。节点顺序唯一且连续；节点内事件可同号表示同时开始。全局交付须覆盖每个事件，不能按文本叙述顺序或块号直接排列。
5. `global.review.*` 独立检查人物/关系/背景与原文是否一致、核心事件有无必要前因或结果缺失、实际发生是否被获知或讲述替代，以及全局故事时间。首轮覆盖全文；之后只处理具体未决问题或修订影响。模型批量提交修正，程序只核验可确定的结构、引用与覆盖，不要求逐候选、逐因果链或逐事件比较的正确证明表。
6. Exporter 投影已保存资料，并按已确认故事顺序回放 delta，生成每个节点入口的 `state_before.json`。`timeline` 下是按顺序排列的时间节点，每节点包含事件；没有通过时间审读的 order 保持 `null`。未完成事实、候选和失败职责进入 `incomplete.json`，不会凭空补造输出。状态缺项、引用失效或同时更新冲突沿用全局检查反馈与修复，不新增模型阶段。

“核心事件”指改变或删去后会改变故事主要因果链、转折、人物命运或结局的实际发生。`core_reason` 说明具体反事实影响。死亡与获知死讯、实施与申请、真实经历与后来的讲述分别建模；`story_spine` 和初提候选只是查漏线索，最终以原文为准。人物档案需记录身份、经历及有方向和适用阶段的关系，地点与世界背景也需保留原文支持的细节。

校验是辅助：程序不代替模型判断语义，模型也不重复检查 JSON 或枚举式结构。模型发现具体错误后提交小范围修订；没有新依据或实质进展时停止机械往返并保存 `partial`。普通抽取最多初次请求加5次修复，校验输出最多初次请求加2次格式修复，全局时间与检查通常合计最多8轮；上限处刚保存实质修订或排时且确需复核时，有界完成交接。倒数两轮检查保存新事件且仍需归时时，也保留有界排时与核对。这些上限用于止损，不是降低提取标准。完整JSON候选的格式修复仍可紧凑提交；响应截断或没有可用候选时，重试携带完整原文与当前资料，退出恢复后沿用同一规则。关系和状态变化采用增量更新，替换事件须由检查角色显式删除旧引用、提交有据新引用及保留对应delta，程序不自动决定跨语义迁移。删除通过当前world中的id与reason定位，无需重抄签名；历史显式签名仍核对。初提身份有错仍用新编号替换并处理引用。同一发生合并若旧时间位置不同，须同时交完整time，组合检查失败不保存这项合并；不同发生不可因此强行合并。

## 代码职责

入口 `main.py` → `workflows/NovelExtractWorkflow.py`（唯一工作流、阶段、并行、恢复）→ `agents/NovelExtractAgent.py`（独立 AutoGen 会话）→ `model/ModelClient.py`（HTTP和累计请求）→ `storage/novel_extract/NovelExtractStorage.py`（事务、锁、快照）→ `exporters/NovelExtractExporter.py`（输出）。`common/novel_extract/NovelExtractSchema.py` 定义领域协议、引用与时间结构检查；`NovelExtractRevision.py` 只在副本上应用事实、时间和档案修订；`NovelExtractState.py` 只处理状态引用、相容 delta 合并和确定性回放，不推断事实或调用模型；Reader 保存原文块和证据位置；Log 写脱敏日志。历史库、报告、许可与配置是复现资料，不按是否被 import 判断有无用途。

## Repository 动态资产契约

新工作区按用户确认的结构输出，不另加 `schema_version` 文件字段。人物、地点、世界背景保持原字段，人物文件移除不区分阶段的 `relation`；人物传记仍保留。全文关系经历只在内部用于导航，当前关系以快照与 delta 为准。

```text
repository/<输出名>/
  roles/<人物ID>/role_info.json
    role_code, role_name, source, activity, nickname, profile,
    gender, identity, age, personality, biography
  objects/<物品ID>/object_info.json
    object_code, object_name, source, aliases[], description,
    function, significance
  locations/<原文名称>.json
    <地点ID>: {location_code, location_name, source, description, detail}
  worlds/background/general.json
    source, world_name, language, description, detail
  worlds/settings/<原文名称>.jsonl
    每行 {term, nature[], detail[]}
  timeline/timeline.json
    source, time_basis:"story", nodes[]
      node_id, order, node_name,
      time:{relative, exact_start:null, exact_end:null, duration:null},
      interval_from_previous:null, path, state_path
  timeline/<节点ID>_<节点名称>/state_before.json
    node_id, previous_node_id, relations[], objects{}
  timeline/<节点ID>_<节点名称>/todolist.json
    node_id, todolist[]
      task_id, order, todo, time, locations[],
      participants:[{role_code, role_name, original_goal}],
      items[], object_ids[], required_results[], state_delta:{relations[], objects[]}
```

`path` 和 `state_path` 都相对于 `timeline/`。`state_before` 覆盖全文目录中全部物品和已识别的人物关系，包括未参与当前节点事件的对象。关系行是 `{owner_id, other_id, relation, status, detail}`，`relation` 表示 other 相对 owner 的单项关系，status 为 `active/inactive/unknown`；三项身份键共同区分关系。物品按 ID 存 `{owner, holder, location, condition}`，分别表示所有者、实际持有者、所在地和有据状况。前三项使用 `ReferenceState={status:"known"|"none"|"unknown", id:string|null}`；known 必须有编号，none 表示明确没有，unknown 表示原文无法确定该时点，两者 id 均为 null。初始物品整项漏填、已识别关系端点未交付均进入既有检查修复，不能用导出默认unknown代替提取；关系具体类型和阶段仍由模型依据原文核对。

事件的关系 delta 行是 `{owner_id, other_id, relation, set:{status?, detail?}, basis}`；物品 delta 行是 `{object_id, set:{owner?, holder?, location?, condition?}, basis}`。set 至少改一项，未出现的字段保持不变；detail/condition 的显式 null 表示改为未知。原文在某事件首次确认的状态也更新记录，basis区分“此时确认”和“此事导致改变”，不能编造未叙述的取得过程或把晚期已知值前置。空 delta 表示已核对且无变化；内部 null 表示尚未交付，不能算完成。

新repository的所有文件均不输出 `evidence` 字段。智能体仍提交原文片段编号，程序回填证据并保存在SQLite、内部检查上下文及外部诊断资料中，供核对与恢复；去掉公开字段不降低内部证据要求。历史格式38的已有文件与只读投影保留原契约，不重写旧实验。

第一节点快照来自有据初始状态；下一节点快照来自前一节点的所有事件变化。同节点同 order 的事件共享同一前态，组内 delta 同时合并：不相干字段可合并，相同值去重，冲突值必须交检查角色修正，不能按事件编号覆盖。任一事件前态等于节点入口快照累计应用本节点所有更早order组的delta，不是只叠加紧邻前一件事件。末节点的最后一组也会回放校验，但不添加虚构尾节点。状态修订不自动撤销正确时间确认；同一发生合并须保全相容 delta，跨语义替代仍由检查角色明确处理。

`partial` 仍保留诊断输出；不可确定的状态路径为 null，不生成依赖未导出事件或未确认顺序的后续快照。这不等于 complete。下游读取新契约时应区分未知、明确不存在及未完成。

## 运行与验证

使用项目 `.venv` 和显式配置。本地配置不提交到仓库；首次运行先复制示例，再按需修改并设置其中 `api_key_env` 指定的环境变量：

```bash
cp model/config.example.json model/config.local.json
```

以下示例为离线预检，**不启动实验**：

```bash
.venv/bin/python -B main.py \
  --input novel_data/作品.txt \
  --config model/config.local.json \
  --workspace .novel_agent_work/作品_新实验.sqlite3 \
  --output repository/作品_新实验 \
  --log-dir logs/novel_extract/作品_新实验 \
  --max-steps 1000 --concurrency 16 --preflight
```

真实运行须另获授权并去掉 `--preflight`。密钥只从配置指定的环境变量读取，不写入命令或聊天。`--max-steps`限制累计 HTTP 尝试（含重试和未知请求），不代表金额。只读状态命令：

```bash
.venv/bin/python -B main.py --workspace .novel_agent_work/作品_实验.sqlite3 --status
```

新工作区格式 **39**；格式1—38仅可只读查看，不能直接用新代码续跑或把旧全文关系推断成动态状态。新契约实验须用新路径。相同输入、配置、代码指纹和路径的 `working` 工作区可原命令恢复；`complete/partial` 终态同命令零模型调用核对输出，不覆盖被改动的文件。退出码：0 完成，2 部分完成，75 运行条件暂停，130 用户中断，1 配置或路径错误。

离线回归：

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

测试使用固定回复和本机 MockTransport，不发付费请求。它们检查结构、原文引用、时间归组、并发、恢复及输出契约，不能证明真实模型的核心事件理解、倒叙判断、运行时间或重复实验稳定性。本地实验记录不随源码提交；语义核对仍需参照 [人工验收表](tests/semantic_acceptance.md)。来源和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
