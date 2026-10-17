# repository 输出契约

本文件供程序维护与验收，不作为模型重写最终结果的提示。根目录固定为 `roles/`、`locations/`、`worlds/`、`timeline/`。原文文件 stem 写入 source。

## 人物

`roles/<role_code>/role_info.json` 恰好十二字段：

`role_code, role_name, nickname, source, activity, profile, gender, identity, age, personality, biography, relation`。

role_code、role_name、source 为字符串；activity 为“主角”或“npc”；identity 为字符串数组或 null；其余资料字符串或 null。relation 为按另一人物编号索引的对象或 null，每项恰好 `relation`（非空字符串数组）、`detail`（字符串或 null），方向表示对方相对于当前人物的关系。

## 地点与世界

- `locations/<source>.json`：按地点编号索引。每项恰好 `location_code, location_name, source, description, detail`。前三项字符串，后两项字符串或 null。
- `worlds/background/general.json`：恰好 `source, world_name, language, description, detail`，source 字符串，其余字符串或 null。不把作品标题当世界名称，不把文本语言当世界语言。
- `worlds/settings/<source>.jsonl`：每行恰好 `term, nature, detail`，term 字符串，后两项字符串数组或 null。

## 时间线

`timeline/timeline.json` 恰好 `source, time_basis, nodes`，time_basis 固定 `story`。

每个节点恰好 `node_id, order, node_name, time, interval_from_previous, path`。

- node_id/node_name/path 是字符串。path 相对于 timeline 目录，直接指向该节点的 todolist.json。
- order 为正整数或 null；不同节点的非空 order 唯一。
- time 恰好 `relative, exact_start, exact_end, duration`，均为字符串或 null。
- interval_from_previous 为 null，或恰好 `duration, relative` 且至少一项为已知字符串。

节点列表文件恰好 `node_id, todolist`。每项任务恰好八字段：

`task_id, order, todo, time, locations, participants, items, required_results`。

- task_id/todo 字符串；todo 保留事件和真实性限定，不把实际前事变成讲述。
- order 为正整数或 null；**同节点相同非空 order 表示同时开始**，null 不表示同时。
- time 是事件自己的原文时间线索字符串或 null，不由节点时间代填。
- locations 为地点名称数组或 null；items/required_results 为字符串数组或 null。
- participants 为数组，每项恰好 `role_code, role_name, original_goal`，前两项字符串且引用有效，original_goal 字符串或 null。

本实现原样投影模型的节点分组；确认后节点order唯一，节点内事件order独立，可为1、2、2、3，其中两个2同时开始。不同节点的事件order不可直接比较。partial保留模型节点ID、名称和事件归属，以及仍有效的独立确认顺序；未确认order输出null，未归入节点的事件保存在incomplete清单，禁止逐事件制造待定节点。清单保留完整节点草稿、事件及核实状态，partial不代表事实或时间已确认。节点时间标签来自模型原文判断，程序不归纳节点、不推算日期或间隔。
