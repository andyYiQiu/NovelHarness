# NovelHarness

本系统是上游抽取系统：按小说原文建立人物、地点、世界背景、核心事件及故事时间线，完整交付 `repository` 和可关联查询的 SQLite。技术路线是：**全文联合提取与核对 → 文本分块 → 两遍核心事件、关系和局部时间提取 → 结构修复 → 跨块合并与故事时间校验 → complete repository → SQLite**。使用 AutoGen；参照 [Shadow-Loom](https://github.com/dwlmt/shadow-loom) 的抽取、结构修复和跨块时间路线，自行实现所需部分。

提取完整性以原文支持的信息是否进入对应字段衡量，不以非空率衡量。汇集全文中同一对象的资料，保留相对年龄、约略时间、时期及转述限定；正文或传记提到的目的、结果、地点、物品状态仍须填写对应字段。已有核对负责补齐有据遗漏；确无依据的字段保持未知，不新增填表事件、检查阶段或非空门槛。`required_results`保存有据的实际结果与直接后果，`original_goal`保存参与者当时目的，二者不能混用。契约中保留的精确起止、时长、节点间隔仍为null，不为减少空值扩展时间研究范围。

## 工作流程

1. `foundation.extract` 一次识别全文人物、别称、地点、物品和背景，同时形成详细人物档案、有向人物关系经历、物品档案、故事主干及 `initial_state`。初始状态对应故事实际起点之前，不能抄用叙述开头或全文结尾的状态。全文带稳定来源标记，供物品和状态引用。`foundation.check` 独立对照原文，提交具体缺漏或错误的增量修订。合法目录、档案草稿和失败记录分别保存；前置资料不完整时仍处理无依赖的后续文本块。
2. Reader 保留原文位置并分块。不同块可以并行；每块依次执行 `reading → first_pass → second_pass → consequences`。前两遍都遵循同一核心事件标准，职责分别偏重行动/经历及言语/关系/遗漏，不能省略其一。模型引用片段编号，程序回填原文；第二遍对同一发生的事实补充作为检查建议保存，不直接覆盖旧事实，也不另建重复事件；同编号且发生身份未变的局部时间位置可修正并校验。无法确定同一性时仍保留新事件供核对。
3. `consequences` 在既有后果提取中为每个事件提交关系和物品的 `state_delta`，只写变化字段；无变化也明确交空集合。程序检查 JSON、字段、编号、原文引用、事件图与时间结构，并给出精确修复反馈。确定性格式错误有限重交；语义疑点保留给有原文上下文的检查。失败块不阻止无依赖的其他块，候选和有效进展保存在 SQLite。
4. `global.timeline.*` 用各块局部时间草稿和全文，将现存事件归入按**故事实际开始时间**排列的节点。节点顺序唯一且连续；节点内事件可同号表示同时开始。全局交付须覆盖每个事件，不能按文本叙述顺序或块号直接排列。
5. `global.review.*` 独立检查人物/关系/背景与原文是否一致、核心事件有无必要前因或结果缺失、实际发生是否被获知或讲述替代，以及全局故事时间。首轮覆盖全文；之后只处理具体未决问题或修订影响。模型批量提交修正，程序只核验可确定的结构、引用与覆盖，不要求逐候选、逐因果链或逐事件比较的正确证明表。
6. Exporter 投影已保存资料，并按已确认故事顺序回放 delta，生成每个节点入口的 `state_before.json`。`timeline` 下是按顺序排列的时间节点，每节点包含事件；没有通过时间审读的 order 保持 `null`。未完成事实、候选和失败职责进入 `incomplete.json`，不会凭空补造输出。状态缺项、引用失效或同时更新冲突沿用全局检查反馈与修复，不新增模型阶段。
7. 运行时指定 `--database`，工作流在 complete 导出并关闭工作区后调用 ImportRepository：核对导出收据→写入公开 SQLite。partial 不入库；入库成功才返回0。入库失败保留 complete 提取结果，修复后原命令只继续入库，不重新调用提取模型。省略 `--database` 时保留原来的仅提取用法。

“核心事件”指改变或删去后会改变故事主要因果链、转折、人物命运或结局的实际发生。`core_reason` 说明具体反事实影响。死亡与获知死讯、实施与申请、真实经历与后来的讲述分别建模；`story_spine` 和初提候选只是查漏线索，最终以原文为准。人物档案需记录身份、经历及有方向和适用阶段的关系，地点与世界背景也需保留原文支持的细节。

校验是辅助：程序不代替模型判断语义，模型也不重复检查 JSON 或枚举式结构。模型发现具体错误后提交小范围修订；没有新依据或实质进展时结束正常全局阶段，转入下述有界补救。普通抽取最多初次请求加5次修复，校验输出最多初次请求加2次格式修复，全局时间与检查通常合计最多8轮；上限处刚保存实质修订或排时且确需复核时，有界完成交接。倒数两轮检查保存新事件且仍需归时时，也保留有界排时与核对。这些上限用于止损，不是降低提取标准。完整JSON候选的格式修复仍可紧凑提交；响应截断或没有可用候选时，重试携带完整原文与当前资料，退出恢复后沿用同一规则。关系和状态变化采用增量更新，替换事件须由检查角色显式删除旧引用、提交有据新引用及保留对应delta，程序不自动决定跨语义迁移。删除通过当前world中的id与reason定位，无需重抄签名；历史显式签名仍核对。初提身份有错仍用新编号替换并处理引用。同一发生合并若旧时间位置不同，须同时交完整time，组合检查失败不保存这项合并；不同发生不可因此强行合并。


正常流程（通常8轮及已有必要交接）结束后，若完成检查仍不通过，在发布终态输出前追加**一次、最多6轮**未完成任务补救。沿用已有检查与时间任务，只针对未决问题、失败候选和拒收回执修订；需要排时和独立复核时共用这6轮。原先因重复无进展而停止的结果也获得这次机会，补救期不会仅因重复签名提前消耗掉其余机会，但不会再次追加配额。通过原有完成条件即停止；6轮后仍未完成则保留 partial，不能入库。

补救期间工作区保持 working，`--status` 的 `review_state.recovery_start_round` 与累计 `rounds` 记录已用机会；日志阶段显示“未完成任务补救”及第几轮。中断恢复沿用已保存进度、候选和请求，不重新提取全文或重置6轮。每轮是一次检查或排时业务交付，可能有原有格式修复和接口重试；累计请求/费用限制仍优先，额度不足就暂停。已发布的历史 complete/partial 仍按原逻辑零调用核对，不能靠重复命令不断领取6轮。新代码与旧指纹工作区不能混用。


修复采用现有提示中的六步方法：确认实际保存状态→定位失败原因→回查原文依据→选择现有修订操作→检查连带影响并提交→读取程序回执。字段修订、事件替换、引用处理、时间重排和状态变化各用已有协议；同一原因的关联修改一次交齐，互不冲突的修订可批量处理。回执提供接收/拒收项、未接收候选及修改目标和关联事件的当前引用，成功修订和单独时间拒收也反馈；索引不是全部影响范围，仍以当前world/information与原文为准。拒收后检查实际保存结果再换用有据方案，不原样重发，也不以改写说明或清空有效事实掩盖问题。分析在当前任务内完成，输出仍只有原有JSON修订和简要依据，不新增长篇推理、计划字段或模型阶段。

## 代码职责

入口 `main.py` → `workflows/NovelExtractWorkflow.py`（唯一工作流、阶段、并行、恢复及完整交付）→ `agents/NovelExtractAgent.py`（独立 AutoGen 会话）→ `model/ModelClient.py`（HTTP和累计请求）→ `storage/novel_extract/NovelExtractStorage.py`（事务、锁、快照）→ `exporters/NovelExtractExporter.py`（输出）。Workflow 的 `prepare_paths` 检查输入和产物路径，`finish_repository` 只调用 ImportRepository，SQL 和原子入库细节仍在 tools 中。`common/novel_extract/NovelExtractSchema.py` 定义领域协议、引用与时间结构检查；`NovelExtractRevision.py` 只在副本上应用事实、时间和档案修订；`NovelExtractState.py` 只处理状态引用、相容 delta 合并和确定性回放，不推断事实或调用模型；Reader 保存原文块和证据位置；Log 写脱敏日志。历史库、报告、许可与配置是复现资料，不按是否被 import 判断有无用途。

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
      task_id, order, todo, epistemic_status, time, locations[],
      participants:[{role_code, role_name, original_goal}],
      items[], object_ids[], required_results[], state_delta:{relations[], objects[]}
```

`todo` 只保存事件描述；`epistemic_status` 单独保存“事实、自述、传闻、猜想、计划”等依据性质及必要说明，直接来自内部同名字段，不是原文证据。事件描述中影响事实理解的转述、不确定性限定仍保留。旧格式的历史输出不改写。

`path` 和 `state_path` 都相对于 `timeline/`。`state_before` 覆盖全文目录中全部物品和已识别的人物关系，包括未参与当前节点事件的对象。关系行是 `{owner_id, other_id, relation, status, detail}`，`relation` 表示 other 相对 owner 的单项关系，status 为 `active/inactive/unknown`；三项身份键共同区分关系。物品按 ID 存 `{owner, holder, location, condition}`，分别表示所有者、实际持有者、所在地和有据状况。前三项使用 `ReferenceState={status:"known"|"none"|"unknown", id:string|null}`；known 必须有编号，none 表示明确没有，unknown 表示原文无法确定该时点，两者 id 均为 null。初始物品整项漏填、已识别关系端点未交付均进入既有检查修复，不能用导出默认unknown代替提取；关系具体类型和阶段仍由模型依据原文核对。

事件的关系 delta 行是 `{owner_id, other_id, relation, set:{status?, detail?}, basis}`；物品 delta 行是 `{object_id, set:{owner?, holder?, location?, condition?}, basis}`。set 至少改一项，未出现的字段保持不变；detail/condition 的显式 null 表示改为未知。原文在某事件首次确认的状态也更新记录，basis区分“此时确认”和“此事导致改变”，不能编造未叙述的取得过程或把晚期已知值前置。空 delta 表示已核对且无变化；内部 null 表示尚未交付，不能算完成。

新repository的所有文件均不输出 `evidence` 字段。智能体仍提交原文片段编号，程序回填证据并保存在SQLite、内部检查上下文及外部诊断资料中，供核对与恢复；去掉公开字段不降低内部证据要求。历史格式38的已有文件与只读投影保留原契约，不重写旧实验。

第一节点快照来自有据初始状态；下一节点快照来自前一节点的所有事件变化。同节点同 order 的事件共享同一前态，组内 delta 同时合并：不相干字段可合并，相同值去重，冲突值必须交检查角色修正，不能按事件编号覆盖。任一事件前态等于节点入口快照累计应用本节点所有更早order组的delta，不是只叠加紧邻前一件事件。末节点的最后一组也会回放校验，但不添加虚构尾节点。状态修订不自动撤销正确时间确认；同一发生合并须保全相容 delta，跨语义替代仍由检查角色明确处理。

`partial` 仍保留诊断输出；不可确定的状态路径为 null，不生成依赖未导出事件或未确认顺序的后续快照。这不等于 complete。下游读取新契约时应区分未知、明确不存在及未完成。

## Repository 导入 SQLite

`tools/ImportRepository.py` 是 SQLAlchemy ORM 导入工具，可由工作流收尾调用，也可独立执行。数据只来自指定的公开 repository；另以只读方式核对对应实验工作区的完成状态、输出路径和导出收据，不加载原文、候选或日志，不调用模型，也不补充 repository 没有的事实或事件间关系。`worlds/` 的背景 JSON 和设定 JSONL 一并存入 `world_data`，原文件保留。

仅上游 `complete` 可以入库。`partial/working`、缺失状态或收据、路径不匹配、导出文件与收据哈希不一致，以及仍存在 `.incomplete.json` 的结果均拒收；之后仍须通过原有结构、顺序和引用检查。`--workspace` 必填，不提供跳过完成检查的选项。工作区状态不写入公开数据库。partial 的工作区和 repository 保留用于诊断，但不会创建下游数据库。

全项目依赖统一在根目录 `requirements.txt`。独立入库时，在 repository 已停止写入后执行：

```bash
.venv/bin/python -B tools/ImportRepository.py \
  --repository repository/作品_实验 \
  --workspace .novel_agent_work/作品_实验.sqlite3 \
  --database databases/作品_实验.sqlite3
```

一个 repository 对应一个新数据库，采用 SQLite＋关联表＋JSON：十三张公开资料表。`tools/database/table/` 中每张表由一个独立文件中的 Python 类定义，继承 `Base`，字段、外键和索引集中在对应类中。需要 SQLite 3.31 或更新版本，使用生成列约束引用的资产类型。

文件职责：`database/Database.py` 定义模型基类和引擎配置；`database/table/<模型类名>.py` 各自定义一张表；`database/table/__init__.py` 只集中注册与导出类；`ImportRepository.py` 读取、检查文件并通过 `Session` 按类批量写入；`RepositoryReader.py` 保留标准库只读查询与状态回放。模块被 import 时不创建数据库或打开全局会话。原 `RepositorySchema.sql` 已由模型类替代，不维护两套表定义。普通文件使用大驼峰命名，`__init__.py` 为 Python 标准包入口。

模型类与表对应如下：

| 模型类 | 数据表 |
| --- | --- |
| `RepositoryInfo` | `repository_info` |
| `WorldData` | `world_data` |
| `Asset` | `assets` |
| `TimeNode` | `time_nodes` |
| `Event` | `events` |
| `StateSnapshot` | `state_snapshots` |
| `EventParticipant` | `event_participants` |
| `EventObject` | `event_objects` |
| `EventLocation` | `event_locations` |
| `RelationSnapshot` | `relation_snapshots` |
| `ObjectSnapshot` | `object_snapshots` |
| `RelationChange` | `relation_changes` |
| `ObjectChange` | `object_changes` |

| 表 | 可直接查询的列 | 保存内容或关联方式 |
| --- | --- | --- |
| `repository_info` | `source, time_basis` | 无额外元数据 |
| `world_data` | `file_path` | `content_json`：背景对象，或按原行顺序保存的设定数组 |
| `assets` | `asset_type, asset_id, name` | `data_json`：人物、地点或物品档案 |
| `time_nodes` | `node_id, node_order, node_name, time_relative` | `data_json`：原始节点 |
| `events` | `task_id, node_id, event_order, todo, epistemic_status, time_text` | `data_json`：完整事件，含稀疏 `state_delta` |
| `state_snapshots` | `node_id, previous_node_id` | `data_json`：完整 `state_before` |
| `event_participants` | `task_id, position, role_id, original_goal` | 通过人物外键读取档案 |
| `event_objects` | `task_id, position, object_id` | 通过物品外键读取档案 |
| `event_locations` | `task_id, position, location_id` | 通过地点外键读取档案 |
| `relation_snapshots` | `node_id, owner_id, other_id, relation, status, detail` | 入口快照中的每条有向人物关系 |
| `object_snapshots` | `node_id, object_id, owner_status, owner_id, holder_status, holder_id, location_status, location_id, condition` | 入口快照中的每件物品状态 |
| `relation_changes` | `task_id, owner_id, other_id, relation, field, value, basis` | 每个实际变化字段一行 |
| `object_changes` | `task_id, object_id, field, ref_status, ref_id, text_value, basis` | 每个实际变化字段一行 |

`asset_type` 为 `role/location/object`。关联表的类型列由数据库生成，不能把人物引用连到物品；人物关系方向保持原样。`position` 从1开始，保留原数组顺序和重复项。事件地点按原名称精确匹配唯一地点 ID，同名歧义拒收；物品只使用 `object_ids`，不从 `items` 猜编号。主键、外键和常用反向查询索引见模型类；使用其他写库工具时也须先执行 `PRAGMA foreign_keys=ON`。来源表始终只导入一行，`RepositoryInfo` 以 `source` 作为 ORM 身份，保留既有物理表结构。

索引与关联列来自同一份原记录，JSON 保留原字段、null、空集合和省略字段。关系变化中没有某字段对应的行表示不变，`detail/value=NULL` 表示明确更新为空；物品 `condition/text_value=NULL` 同理。引用状态保留 `known/none/unknown`，未知不能冒充无所有者或无所在地。导入不重算快照、不生成新事实；这些表是公开数据的只读查询投影，不提供两套内容分别更新的接口。旧 repository 未单列依据性质时，新库的 `events.epistemic_status` 为 NULL，原 `todo` 与 JSON 原样保留，不猜拆历史文本；已有数据库不自动迁移或覆盖。

导入前检查当前公开契约的文件覆盖、编号、引用、两层顺序和状态结构；缺文件、空 order、空事件列表、悬空引用、重复 JSON 键或未识别的数据文件均明确报错，不跳过问题记录。合法的未知状态与可空描述允许保留。导入成功只表示这些存储检查通过，不替代原文语义验收，也不将上游 `partial` 改称 `complete`。

建表及写入在同一个 `Session` 事务内完成，显式启用 SQLite 外键和事务，使建表也能回滚。按模型批量插入，显式保留 null，空集合不发 INSERT；不逐条 commit。释放连接后才原子发布；失败不留下半份目标库，已有目标一律拒绝覆盖。独立 CLI 重复导入需指定新路径。工作流恢复调用 `reuse_existing=True` 时，先按当前 complete repository 在临时库生成同一份公开投影，逐表比对已有库；十三张公开资料表完全一致才复用，已有数据库不写入、不改表。不同或损坏的库拒收，不能凭文件名当成已入库。工具不追加或修改原事实。退出码为 0（成功）或 1（导入失败）；参数用法错误为 2。

`tools/RepositoryReader.py` 提供只读查询，禁止写库且不会创建不存在的文件：

```python
from tools.RepositoryReader import RepositoryReader

with RepositoryReader("databases/作品_实验.sqlite3") as reader:
    context = reader.get_event_context("事件ID")  # 原事件、节点、完整人物/物品/地点档案
    history = reader.get_role_events("人物ID")     # 实际参与的事件，不包括只被状态提及
    objects = reader.get_object_history("物品ID")  # 档案、各节点入口状态、逐字段变化
    before = reader.get_state_before("事件ID")     # relations 列表和 objects 映射
    after = reader.get_state_after_group("节点ID", 1)
```

状态查询从该节点入口快照开始，只累计本节点更早的 order 组。相同 order 的事件共享前态，整组相容变化合并后才进入下一组；冲突报错，不按事件编号覆盖。新出现的关系未声明状态时仍为 `unknown`。跨节点直接读取已有入口快照，工具不补造快照或推测时间。同组结果以编号稳定展示时，展示先后不代表时间先后。

每个事件的完整 `state_delta` 保留在 `events.data_json`，其中每个实际变化字段另外投影到 `relation_changes` 或 `object_changes`，通过 `task_id` 关联事件。无变化的事件保留原始空 delta，不制造变化行。事件前态＝节点入口快照依次应用更早 order 组的 delta；组后态再应用当前整组相容 delta。这里“应用”是按字段更新，不是数字相加；没有精确时间依据时不推断任意钟表时刻的状态。

终端可直接检查数据库：

```bash
sqlite3 -readonly databases/作品_实验.sqlite3
```

```sql
.tables
.schema event_participants
PRAGMA table_xinfo(event_participants);
.headers on
.mode box
SELECT * FROM event_participants;
SELECT e.task_id, e.todo, a.name, p.original_goal
FROM events e JOIN event_participants p ON p.task_id=e.task_id
JOIN assets a ON a.asset_type=p.role_type AND a.asset_id=p.role_id
JOIN time_nodes n ON n.node_id=e.node_id
ORDER BY n.node_order, e.event_order, e.task_id, p.position;
.quit
```

## 运行与验证

使用 Python 3.11+、项目 `.venv` 和根目录唯一的依赖文件。提取和入库的已验证版本统一维护，不另设 tools 内依赖清单：

```bash
.venv/bin/python -m pip install -r requirements.txt
```

本地配置不提交到仓库；首次运行先复制示例，再按需修改并设置其中 `api_key_env` 指定的环境变量：

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
  --database databases/作品_新实验.sqlite3 \
  --max-steps 1000 --concurrency 16 --preflight
```

这是一条完整流程的预检命令：检查依赖、配置、分块和路径，拒绝新实验占用已有数据库或把产物写入原文、配置与工作区；不创建工作区、不调用 API。正式执行去掉 `--preflight`，须另获实验授权。

指定 `--database` 时，成功产物为 repository 和该 SQLite。省略此参数则仅提取。工作区的 `complete` 仍表示小说提取及 repository 完成，入库失败不会把它倒退为 working；整条命令返回1并说明失败位置。原命令重试会逐表核对已有公开库，仅内容完全一致才复用，全部成功才打印“完整交付”并返回0。

密钥只从配置指定的环境变量读取，不写入命令或聊天。`--max-steps`限制累计 HTTP 尝试（含重试和未知请求），不代表金额。只读状态命令：

```bash
.venv/bin/python -B main.py --workspace .novel_agent_work/作品_实验.sqlite3 --status
```

新工作区格式 **40**；格式1—39仅可只读查看，不能直接用新代码续跑或把旧全文关系推断成动态状态。工作流与依赖修改会改变运行指纹；历史工作区不直接用新入口续跑，其已有 complete repository 仍可通过独立工具入库，无需重做小说提取。相同输入、配置、代码指纹和路径的 `working` 工作区可原命令恢复；`complete/partial` 终态同命令不新增提取请求，指定 `--database` 的 complete 结果继续核对后处理。退出码：0 请求的流程全部完成，2 提取部分完成，75 运行条件暂停，130 用户中断，1 配置、路径或后处理错误。

离线回归：

```bash
.venv/bin/python -B -m unittest discover -s tests -p '[Tt]est*.py' -v
```

测试使用固定回复和本机 MockTransport，不发付费请求。它们检查结构、原文引用、时间归组、并发、恢复及输出契约，不能证明真实模型的核心事件理解、倒叙判断、运行时间或重复实验稳定性。本地实验记录不随源码提交；语义核对仍需参照 [人工验收表](tests/semantic_acceptance.md)。来源和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
