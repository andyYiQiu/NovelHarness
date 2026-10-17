# NovelHarness

按小说原文建立人物、地点、世界背景、核心事件及故事时间线。技术路线是：**全文联合提取与核对 → 文本分块 → 两遍核心事件、关系和局部时间提取 → 结构修复 → 跨块合并与故事时间校验 → repository**。使用 AutoGen；参照 [Shadow-Loom](https://github.com/dwlmt/shadow-loom) 的抽取、结构修复和跨块时间路线，自行实现所需部分。

## 工作流程

1. `foundation.extract` 一次识别全文人物、别称、地点、物品和背景，同时形成详细人物档案、有向人物关系及故事主干。`foundation.check` 独立对照原文，提交具体缺漏或错误的增量修订。合法目录、档案草稿和失败记录分别保存；前置资料不完整时仍处理无依赖的后续文本块。
2. Reader 保留原文位置并分块。不同块可以并行；每块依次执行 `reading → first_pass → second_pass → consequences`。前两遍都遵循同一核心事件标准，职责分别偏重行动/经历及言语/关系/遗漏，不能省略其一。模型引用片段编号，程序回填原文；第二遍对同一发生的事实补充作为检查建议保存，不直接覆盖旧事实，也不另建重复事件；同编号且发生身份未变的局部时间位置可修正并校验。无法确定同一性时仍保留新事件供核对。
3. 程序检查 JSON、字段、编号、原文引用、事件图与时间结构，并给出精确修复反馈。确定性格式错误有限重交；语义疑点保留给有原文上下文的检查。失败块不阻止无依赖的其他块，候选和有效进展保存在 SQLite。
4. `global.timeline.*` 用各块局部时间草稿和全文，将现存事件归入按**故事实际开始时间**排列的节点。节点顺序唯一且连续；节点内事件可同号表示同时开始。全局交付须覆盖每个事件，不能按文本叙述顺序或块号直接排列。
5. `global.review.*` 独立检查人物/关系/背景与原文是否一致、核心事件有无必要前因或结果缺失、实际发生是否被获知或讲述替代，以及全局故事时间。首轮覆盖全文；之后只处理具体未决问题或修订影响。模型批量提交修正，程序只核验可确定的结构、引用与覆盖，不要求逐候选、逐因果链或逐事件比较的正确证明表。
6. Exporter 仅投影已保存资料。`timeline` 下是按顺序排列的时间节点，每节点包含事件；没有通过时间审读的 order 保持 `null`。未完成事实、候选和失败职责进入 `incomplete.json`，不会凭空补造输出。

“核心事件”指改变或删去后会改变故事主要因果链、转折、人物命运或结局的实际发生。`core_reason` 说明具体反事实影响。死亡与获知死讯、实施与申请、真实经历与后来的讲述分别建模；`story_spine` 和初提候选只是查漏线索，最终以原文为准。人物档案需记录身份、经历及有方向和适用阶段的关系，地点与世界背景也需保留原文支持的细节。

校验是辅助：程序不代替模型判断语义，模型也不重复检查 JSON 或枚举式结构。模型发现具体错误后提交小范围修订；没有新依据或实质进展时停止机械往返并保存 `partial`。普通抽取最多初次请求加5次修复，校验输出最多初次请求加2次格式修复，全局时间与检查通常合计最多6轮；仅第6轮才保存新事件且仍需归时时，额外最多一次排时与一次核对。这些上限用于止损，不是降低提取标准。

## 代码职责

入口 `main.py` → `workflows/NovelExtractWorkflow.py`（唯一工作流、阶段、并行、恢复）→ `agents/NovelExtractAgent.py`（独立 AutoGen 会话）→ `model/ModelClient.py`（HTTP和累计请求）→ `storage/novel_extract/NovelExtractStorage.py`（事务、锁、快照）→ `exporters/NovelExtractExporter.py`（输出）。`common/novel_extract/NovelExtractSchema.py` 定义领域协议、引用与时间结构检查；`NovelExtractRevision.py` 只在副本上应用事实、时间和档案修订；Reader 保存原文块和证据位置；Log 写脱敏日志。历史库、报告、许可与配置是复现资料，不按是否被 import 判断有无用途。

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

新工作区格式 **37**；格式1—36仅可只读查看。相同输入、配置、代码指纹和路径的 `working` 工作区可原命令恢复；`complete/partial` 终态同命令零模型调用核对输出，不覆盖被改动的文件。退出码：0 完成，2 部分完成，75 运行条件暂停，130 用户中断，1 配置或路径错误。

离线回归：

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

测试使用固定回复和本机 MockTransport，不发付费请求。它们检查结构、原文引用、时间归组、并发、恢复及输出契约，不能证明真实模型的核心事件理解、倒叙判断、运行时间或重复实验稳定性。本地实验记录不随源码提交；语义核对仍需参照 [人工验收表](tests/semantic_acceptance.md)。来源和许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
