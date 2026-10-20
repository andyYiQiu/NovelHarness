# 离线回归

运行 `.venv/bin/python -B -m unittest discover -s tests -v`。固定回复通过本机 MockTransport 驱动真实 AutoGen、Workflow、SQLite 与 Exporter，不访问外网或真实密钥。

活动测试覆盖全文联合提取、分块两遍事件职责、原文编号与引用、具体修订、时间全局归组和两层 order、倒叙修正、失败隔离、原子恢复、并发预算及 `roles/objects/locations/worlds/timeline` 输出。`test_dynamic_assets.py` 覆盖稀疏delta保存恢复、同时更新冲突、目录合并原子性、节点入口快照、来源和引用、状态修正不重排正确时间及全流程固定回复；没有增加模型阶段。正式79数据库和 repository 存在时，还会只读重放并比较全部39个旧契约输出文件；历史文件不修改。

本轮撤销了逐候选去向、逐因果链绑定、逐事件比较和逐档案审计的模型证明表。依赖这些已废弃提交字段的旧测试保留但显式 `skip`，名单与原因见 [retired_tests.md](../reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md)。新契约的结构、原文覆盖、一次时间语义审读及修订失效由 `test_shadowloom_validation_scope.py` 和其余活动测试检验。测试通过不能证明模型真的找全核心事件或排对故事时间；真实结果需按 [semantic_acceptance.md](semantic_acceptance.md) 人工对照原文。

格式39补充：检查非参与对象持续保留、多个order组累计回放、初始状态漏项与明确未知的区别，以及公开JSON/JSONL任意层级无evidence但内部证据不丢失。固定回复测试验证原有检查能修复漏填且不重建正确时间，不证明真实模型一定能发现语义遗漏。

截断恢复补充：`test_agent_repair_resilience.py`检查缺候选、旧候选、不可解析JSON和中断恢复时完整任务不丢失，正常格式修复仍紧凑，连续截断仍有次数上限。`test_harness.py`检查真实请求上下文（不靠模拟服务缓存补齐）、截断后全流程确认、终态零调用，以及第8轮后的必要有界交接。事件替换仍由既有显式引用修订与拒收回执用例保障。

正式96边界回归覆盖：当前编号删除及历史签名冲突、替换时的显式引用处理、同目标重复删除、跨旧位置的同一发生合并与完整时间修订原子接收/回退、无效时间不得确认旧草稿，以及中断恢复后不重复请求。1/2/4块正常固定回复流程的请求数、最终world和repository哈希与修改前一致。

发布前整理补充：档案合并回退后的时间重放复用同一接收函数；测试覆盖回退导致节点重号时保留旧资料、拒绝新时间确认，防止错误路径与正常路径的保护规则分叉。
