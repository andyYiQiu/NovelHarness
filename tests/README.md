# 离线回归

运行 `.venv/bin/python -B -m unittest discover -s tests -p '[Tt]est*.py' -v`，同时发现现有测试和大驼峰命名的新测试。固定回复通过本机 MockTransport 驱动真实 AutoGen、Workflow、SQLite 与 Exporter，不访问外网或真实密钥。

`TestRepositoryImport.py` 与 `TestRepositoryReader.py` 使用临时目录验证独立 SQLite 工具：complete 状态与导出收据门槛、十三表数据保真、世界资料 JSON/JSONL 保留及缺失/损坏拒收、类型外键、地点名称歧义、数组位置、稀疏 null、事务与原子发布、关联查询、同组共享前态、相容合并与冲突拒绝。SQLAlchemy 模型覆盖全部表，另验证 Session 按主键与关联查询、连接重建后仍启用外键、后期写入失败时建表也回滚。`TestRepositoryCompletion.py` 用固定回复驱动真实工作流，验证 complete 入库、结构合法的 partial 拒收，以及空事件反馈后在原有复核中补齐或保持 partial。只读核对测试工作区，不访问真实模型。先安装 `.venv/bin/python -m pip install -r requirements.txt`；单独运行：`.venv/bin/python -B -m unittest discover -s tests -p 'TestRepository*.py' -v`。

活动测试覆盖全文联合提取、分块两遍事件职责、原文编号与引用、具体修订、时间全局归组和两层 order、倒叙修正、失败隔离、原子恢复、并发预算及 `roles/objects/locations/worlds/timeline` 输出。`test_dynamic_assets.py` 覆盖稀疏delta保存恢复、同时更新冲突、目录合并原子性、节点入口快照、来源和引用、状态修正不重排正确时间及全流程固定回复；没有增加模型阶段。正式79数据库和 repository 存在时，还会只读重放并比较全部39个旧契约输出文件；历史文件不修改。

本轮撤销了逐候选去向、逐因果链绑定、逐事件比较和逐档案审计的模型证明表。依赖这些已废弃提交字段的旧测试保留但显式 `skip`，名单与原因见 [retired_tests.md](../reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md)。新契约的结构、原文覆盖、一次时间语义审读及修订失效由 `test_shadowloom_validation_scope.py` 和其余活动测试检验。测试通过不能证明模型真的找全核心事件或排对故事时间；真实结果需按 [semantic_acceptance.md](semantic_acceptance.md) 人工对照原文。

格式39补充：检查非参与对象持续保留、多个order组累计回放、初始状态漏项与明确未知的区别，以及公开JSON/JSONL任意层级无evidence但内部证据不丢失。固定回复测试验证原有检查能修复漏填且不重建正确时间，不证明真实模型一定能发现语义遗漏。

格式40补充：`test_dynamic_assets.py` 核对事件 description 与 epistemic_status 分列导出；`TestRepositoryImport.py` 核对独立列和原 JSON 保真及旧 repository 缺字段兼容；`TestRepositoryCompletion.py` 验证提取、导出、入库、关联读取的完整通路与终态零新增调用。

截断恢复补充：`test_agent_repair_resilience.py`检查缺候选、旧候选、不可解析JSON和中断恢复时完整任务不丢失，正常格式修复仍紧凑，连续截断仍有次数上限。`test_harness.py`检查真实请求上下文（不靠模拟服务缓存补齐）、截断后全流程确认、终态零调用，以及第8轮后的必要有界交接。事件替换仍由既有显式引用修订与拒收回执用例保障。

正式96边界回归覆盖：当前编号删除及历史签名冲突、替换时的显式引用处理、同目标重复删除、跨旧位置的同一发生合并与完整时间修订原子接收/回退、无效时间不得确认旧草稿，以及中断恢复后不重复请求。1/2/4块正常固定回复流程的请求数、最终world和repository哈希与修改前一致。

发布前整理补充：档案合并回退后的时间重放复用同一接收函数；测试覆盖回退导致节点重号时保留旧资料、拒绝新时间确认，防止错误路径与正常路径的保护规则分叉。

`TestPartialRecovery.py` 验证正常完成零额外请求、原有交接后再补救、补事件→排时→复核、第6轮成功或仍有未归时事件的两种收尾、未完成拒收入库、事务中断的响应复用、请求额度暂停和导出问题共用同一6轮配额。全部使用固定回复与断网保护。

`TestRepairFeedback.py` 验证未接收候选保留稀疏null、接收/拒收分离、关系两端与非参与者状态引用、依据回执显式清理删除引用、单独时间拒收的候选交接、成功修订回执及事务中断恢复。测试只证明反馈与执行通路，不证明模型自主推理的正确率。

`TestEventIndex.py` 验证第十四张派生向量表、显式编号映射、分批中断续建、收据发布、原事实不变、内容变更失效、模型配置拒绝混用、索引损坏拒收及按得分关联资料。固定向量仅测工程通路。需安装 `requirements.txt`；未安装索引依赖时跳过该模块，已有提取和入库测试仍可运行。

真实模型编码/建库/检索集成用例，以及 `TestEventEmbeddingSemantics.py` 的6组中文语义排序对照，只有设置 `NOVEL_JINA_MODEL_PATH` 才运行。模型须预先位于本机，编码进程离线运行：

```bash
NOVEL_JINA_MODEL_PATH="$PWD/.embedding_models/JinaV3" \
  .venv/bin/python -B -m unittest discover -s tests -p 'TestEvent*.py' -v
```

当前真实集成通过；语义对照5/6符合预期，“拒绝包办婚姻并私奔”这一例失败，测试保留失败断言，不能将其当成全部通过。它测的是同义改写优先于表面相似但发生不同的描述，不是完整检索质量基准。默认离线回归不加载真实模型，因此真实模型测试显式跳过；默认通过不代表语义质量通过。

`TestWorkflowDelivery.py` 验证现有 Workflow 的 --database 完整交接：complete 导出→SQLAlchemy 入库→向量缓存→FAISS→关联查询，partial 不执行后处理，编码/索引收据失败后的续做不增加提取请求，已有资料被改动时拒绝复用，预检不写库且缺模型或目标冲突在模型请求前失败。设置 NOVEL_JINA_MODEL_PATH 后还用本地真实 Jina 跑一次固定提取回复的完整链路；它不发提取 API 请求，不是新小说实验。终态重跑必须复用全部向量。
