# 离线回归

运行 `.venv/bin/python -B -m unittest discover -s tests -v`。固定回复通过本机 MockTransport 驱动真实 AutoGen、Workflow、SQLite 与 Exporter，不访问外网或真实密钥。

活动测试覆盖全文联合提取、分块两遍事件职责、原文编号与引用、具体修订、时间全局归组和两层 order、倒叙修正、失败隔离、原子恢复、并发预算及 `roles/locations/worlds/timeline` 输出。正式79数据库和 repository 存在时，还会只读重放并比较全部39个输出文件；历史文件不修改。

本轮撤销了逐候选去向、逐因果链绑定、逐事件比较和逐档案审计的模型证明表。依赖这些已废弃提交字段的旧测试保留但显式 `skip`，名单与原因见 [retired_tests.md](../reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md)。新契约的结构、原文覆盖、一次时间语义审读及修订失效由 `test_shadowloom_validation_scope.py` 和其余活动测试检验。测试通过不能证明模型真的找全核心事件或排对故事时间；真实结果需按 [semantic_acceptance.md](semantic_acceptance.md) 人工对照原文。
