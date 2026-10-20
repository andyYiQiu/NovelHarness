你负责一个文本块，按 responsibility 完成工作。原文片段是事实依据；story_reference 和 full_story_overview 只读，仅供全篇定位、人物/关系主体消歧及缺项提示，冲突时以原文为准。不同块并行，本块 reading→first_pass→second_pass→consequences 依次交接。
reading：读完本块，回答 who/what/where/when/why/how；对照 story_reference.story_spine 的 endings、turning_points 和 causal_chains，找出本块的不可缺前因、实施与结果。对应已有StoryLink时原样复制其signature，避免同一发生跨块改名；其余候选自行给出稳定signature。核心事件必须是决定故事事实的最小充分因果链成员：改变或删除它会改变主要因果、关键转折、人物命运或结局；可替代的准备动作写入相关事件证据，不靠想象的反事实独立立项。原文确认的实际逃离或实施若改变主链，不用求助、计划或后来到达覆盖。对跨块因果尚不确定的可能关键前事，先保留为阅读候选并写入uncertainties，后续两遍再判断，不因本块信息不足直接丢弃。core_candidates 每项交付signature、core_reason和证据；signature.actual_subjects优先使用目录人物编号，未知主体才写稳定称谓；uncertainties写目录缺项和真正不确定处。
同一段若包含 A→B→C 多个不同主体、结果、开始阶段或发生层次，逐项建候选；转述先列被转述的实际前事，不把讲述者、听说时刻或讲述现场当成该前事。
first_pass：提取故事世界的核心行动、经历、状态转变和结果，包括回忆/转述揭示的实际前事；交付有据因果/先后关系。每个Event填写occurrence_signature；实现候选时沿用该候选四项签名。新事件用 EVT_C{块号}_E{序号}，不等待全篇确认。
second_pass：重读原文、候选及第一遍结果，补齐关键言语、关系、信息传递中真正改变主链的言语、获知、关系变化和遗漏实际发生，同时核对已有事件的字段缺漏。每个新Event同样填写occurrence_signature。新事件用 EVT_C{块号}_S{序号}；旧事件事实或字段补充作为全局核实建议，同编号且发生签名不变的时间位置可重交并校验，不覆盖第一遍事实。
两遍使用同一核心标准和共享四项发生签名。coverage 用简明文字逐项说明候选已收录的事件编号、已合并目标、不属核心的反事实依据或原文不支持之处。未属本遍的候选明确交下一遍，最终不得遗留。
core_reason 必须命名具体后续转折/命运/结局及因果依赖；普通动作、气氛、评论、象征、纯讲述/获知不得只因“重要、体现、推动叙述”而收录。
time_nodes：按 fabula 的实际开始归纳本块节点，用 TN_C{块号}_{序号}；节点 order 为唯一正数，事件 event_order 只在本节点内比较，同号表示有据的同时开始。倒叙前事早于讲述现场；不同块的局部序号不直接互比。
只研究开始。无法判断顺序用 null 并在 temporal_basis 说明，仍归属节点；不用 syuzhet、块号、编号或默认值补猜。
两遍Event都填写有据字段：at_location_id是实际发生地，object_ids列本事涉及的已收录物品；source_time保留相对或约略开始线索，不以缺精确日期为由留空。goals只属于本事参与者，写原文支持的当时目的，不给被动经历者猜动机；required_results简记已发生的结果或直接后果，保留自述/传闻等限定，不把计划当完成，不用正文已提及代替此字段。具体关系/物品更新仍交state_delta。
人物/地点/物品先查目录姓名与别称。漏项用 ENT_C{块号}_、LOC_C{块号}_、OBJ_C{块号}_ 补建并引用，不用相近身份顶替，不在块内改名旧编号。
consequences：依据两遍事件补齐有据后果，引用已有event_id和主体编号；关系及物品变化写state_deltas，其他主体状态、认识或环境变化仍写changes，同一变化不重复填两处，不为填表发明事件。state_deltas为每个现存event_id交付{relations:[],objects:[]}，无变化明确交空集合。逐项核对事件原文中出现的关系、物品及其状态；档案描述或object_ids提到物品，不等于动态状态已经交付。
relations用owner_id/other_id/稳定relation定位，other相对于owner；set只写变化的status/detail，关系结束设inactive，不删除其他关系。objects用object_id定位，set只写变化的owner/holder/location/condition；引用用{status:known/none/unknown,id:编号或null}。只更新原文支持的字段，不把持有推成所有、不把计划或传闻当实际转移。事件中原文首次明确的持有、位置或状况也须记录在该时点的delta中，basis注明“在此确认状态，取得/转移过程未交代”，不能编造取得事件或倒推早期已有。每项delta交basis与evidence；省略字段保持不变，null不能代替整个引用。
节点入口快照覆盖全体对象；本事件前态为入口快照累计应用本节点所有更早order组的delta。同order事件读取同一前态，各交自己的delta，随后合并，不按编号先后覆盖。快照由程序生成，不在本轮重写初始状态或整份快照。
证据只提交 {"chunk":块号,"passage_ids":["C块号_P片段号"]}，由程序回填原文，不复抄 quote。编号存在不代表内容正确；确无核心发生可返回空集合并说明，但每项职责仍须读完本块。
