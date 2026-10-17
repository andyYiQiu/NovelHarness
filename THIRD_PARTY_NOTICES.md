# 来源与许可

## 2026-10-15 AutoGen 自实现

参考 David Rae Wilmot 的 [Shadow-Loom](https://github.com/dwlmt/shadow-loom)，固定提交 `2d722e2aa563578d899c7c69758bd874c98ca9b2`（2026-06-18；本轮核对本地源码与远端 trunk 一致）。参考范围为 `shadow_loom/ingestion.py`、`models.py`、`settings.py` 及 `prompts/ontology_*.md`、事件/关系/后果、校验修复提示。

本项目重新编写 AutoGen 工作流、数据协议、会话、存储与导出；运行时不导入 Shadow-Loom，也不保存其整套源码副本。采用全文实体识别、章节/段落分块、问答准备、块内分工与交接、结构反馈、跨块整合及全局时间校验的方法。当前中文角色提示在已有适配方法上重新提炼，保留来源与许可说明；不能因“自行重写”抹去已有来源。

上游声明：**SPDX-FileCopyrightText: 2026 David Rae Wilmot；SPDX-License-Identifier: AGPL-3.0-or-later**。必要许可全文保存在 [LICENSES/AGPL-3.0-or-later.txt](LICENSES/AGPL-3.0-or-later.txt)。相关参考/改编文件：`NovelExtractReader.py`、`NovelExtractWorkflow.py`、`NovelExtractionPrompt.md`、`NovelCheckPrompt.md`、`NovelInformationPrompt.md`；修改和适配由本项目完成，不表示上游作者认可本项目或其结果。

此前两份中文提示及其完整来源说明曾随旧实现归档于 `reports/novel_extract/shadow_loom_rebuild_20261015/before.tar.gz`。本轮过程中尝试过原生框架连接，但依照用户后续要求，最终移除了该连接和 vendor 目录，生产依赖仅为 AutoGen、Pydantic 数据校验及 HTTPX。2026-10-15验收期间发现该旧归档路径失效；当前改动前的可用基线见 `reports/novel_extract/formal71_fixes_20261015/before.tar.gz`。必要许可正文始终保留，不因模块合并移除。

AutoGen 为 Microsoft 的 MIT 许可项目；本项目使用已安装的 `autogen-agentchat`、`autogen-core` 0.7.5，没有复制或修改框架代码。

## 2026-10-13 核心顶层事件方法参考

本次自主编写核心事件判定、反事实说明、故事导航与粒度示例，参考以下研究思想，未复制其实现代码或数据：

- Sharma等，[Once Upon a Kernel: Extracting Important Events from Narratives](https://aclanthology.org/2026.lrec-1.477/)，LREC 2026：kernel/satellite区分、全篇冲突与转折视角。论文主要标注事件中心词，并不直接提供本项目的顶层复合事件结构。
- Otake等，[Modeling Event Salience in Narratives via Barthes’ Cardinal Functions](https://aclanthology.org/2020.coling-main.160/)：事件删除与叙事连贯性的关系。
- Qin等，[Counterfactual Story Reasoning and Generation](https://aclanthology.org/D19-1509/)：反事实改变与受影响后续；本项目不逐候选调用模型生成改写故事。
- Aldawsari与Finlayson，[Detecting Subevents using Discourse and Narrative Features](https://aclanthology.org/P19-1471/)：事件包含层级与因果关系的区分。
- Wilmot与Keller，[Memory and Knowledge Augmented Language Models for Inferring Salience in Long-Form Stories](https://aclanthology.org/2021.emnlp-main.65/)：长篇事件重要性的上下文问题。
- [SemEval 2026 Task 4: Narrative Story Similarity and Narrative Representation Learning](https://narrative-similarity-task.github.io/)及公开标注指南：故事行动进程、结果与表面差异的区分。

LREC论文所链Dataverse发布项元数据显示CC BY-NC 4.0；此次只核对发布元数据，数据包读取返回403，没有读取或复制发布包中的代码、提示文件或标注指南。本文方法说明来自论文正文和附录及上述公开论文，不把论文开放阅读误称为其数据/代码可任意商用。已有Shadow Loom适配内容及AGPL声明继续保留；本次不新增第三方实现依赖。
