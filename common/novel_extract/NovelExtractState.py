"""动态资产的结构检查与确定性回放；不推断关系、归属或故事时间。"""
from itertools import groupby

from common.novel_extract.NovelExtractSchema import (
    AssetState, ObjectState, StateDelta, evidence_errors, evidence_records, timeline_errors,
)


def relation_key(row):
    return row.owner_id, row.other_id, row.relation


def unique_evidence(rows):
    records = {(row.chunk, tuple(row.passage_ids), row.quote): row for row in rows}
    return [records[key] for key in sorted(records)]


def asset_references(value):
    """提取状态中的目录引用，供合并隔离复用；不推断未声明的主体。"""
    value = value.model_dump(mode='json') if hasattr(value, 'model_dump') else value
    result = {'entities': set(), 'locations': set(), 'objects': set()}
    if not isinstance(value, dict):
        return result
    relations = value.get('relations')
    for row in relations if isinstance(relations, list) else []:
        if isinstance(row, dict):
            result['entities'].update(row[key] for key in ('owner_id', 'other_id') if isinstance(row.get(key), str))
    objects = value.get('objects') or {}
    if isinstance(objects, dict):
        rows = objects.items()
    elif isinstance(objects, list):
        rows = ((row.get('object_id'), row.get('set', {})) for row in objects if isinstance(row, dict))
    else:
        rows = ()
    for key, fields in rows:
        if isinstance(key, str):
            result['objects'].add(key)
        if not isinstance(fields, dict):
            continue
        for name, group in (('owner', 'entities'), ('holder', 'entities'), ('location', 'locations')):
            reference = fields.get(name)
            if isinstance(reference, dict) and isinstance(reference.get('id'), str):
                result[group].add(reference['id'])
    return result


def merge_deltas(deltas):
    """同一发生或同时组只合并相容字段；冲突须由检查角色明确修正。"""
    result = StateDelta()
    for field, identity in (('relations', relation_key), ('objects', lambda row: row.object_id)):
        rows = {}
        for delta in deltas:
            if delta is None:
                continue
            for row in getattr(delta, field):
                key = identity(row)
                if key not in rows:
                    rows[key] = row.model_copy(deep=True)
                    continue
                saved = rows[key]
                before, incoming = saved.set.model_dump(), row.set.model_dump()
                conflicts = sorted(name for name in before.keys() & incoming.keys() if before[name] != incoming[name])
                if conflicts:
                    raise ValueError(f'state_delta.{field}.{key}: 同时更新冲突 {conflicts}；'
                                     '依据原文修正delta或同时分组，不能按事件编号覆盖')
                saved.set = type(saved.set).model_validate({**before, **incoming})
                saved.evidence = unique_evidence([*saved.evidence, *row.evidence])
                saved.basis = '\n'.join(dict.fromkeys((saved.basis, row.basis)))
        setattr(result, field, [rows[key] for key in sorted(rows)])
    return result


def remap_assets(value, renames):
    """目录合并时更新已明确的引用，不迁移跨语义事件的状态。"""
    if value is None:
        return None
    value = value.model_copy(deep=True)
    relations = []
    for row in value.relations:
        row.owner_id = renames.get(row.owner_id, row.owner_id)
        row.other_id = renames.get(row.other_id, row.other_id)
        if row.owner_id != row.other_id:
            relations.append(row)
    value.relations = relations
    is_snapshot = isinstance(value, AssetState)
    objects = value.objects.items() if is_snapshot else ((row.object_id, row) for row in value.objects)
    remapped, deltas = {}, []
    for key, row in objects:
        key = renames.get(key, key)
        fields = row if is_snapshot else row.set
        for name in ('owner', 'holder', 'location'):
            reference = getattr(fields, name)
            if reference is not None and reference.id is not None:
                reference.id = renames.get(reference.id, reference.id)
        if not is_snapshot:
            row.object_id = key
            deltas.append(row)
            continue
        if key in remapped:
            old = remapped[key]
            if old.model_dump(exclude={'evidence'}) != row.model_dump(exclude={'evidence'}):
                raise ValueError(f'物品合并后状态冲突：{key}；同轮明确提交合并后的initial_state')
            row.evidence = unique_evidence([*old.evidence, *row.evidence])
        remapped[key] = row
    value.objects = remapped if is_snapshot else deltas
    if not is_snapshot:
        return merge_deltas([value])
    grouped = {}
    for row in value.relations:
        key = relation_key(row)
        if key in grouped:
            old = grouped[key]
            if (old.status, old.detail) != (row.status, row.detail):
                raise ValueError(f'人物合并后初始关系冲突：{key}；明确提交合并后的initial_state')
            old.evidence = unique_evidence([*old.evidence, *row.evidence])
        else:
            grouped[key] = row
    value.relations = list(grouped.values())
    return value


def state_reference_errors(value, register, label):
    errors, identities = [], set()
    for row in value.relations:
        key = relation_key(row)
        if row.owner_id == row.other_id or {row.owner_id, row.other_id} - register.entities.keys():
            errors.append(f'{label}.relations.{key}: 关系端点须为两个已知人物')
        if key in identities:
            errors.append(f'{label}.relations.{key}: 重复关系，须合成一项明确更新')
        identities.add(key)
    snapshot = isinstance(value, AssetState)
    objects = value.objects.items() if snapshot else ((row.object_id, row.set) for row in value.objects)
    for key, fields in objects:
        if key not in register.objects:
            errors.append(f'{label}.objects.{key}: 未知物品编号')
        for name, catalog in (('owner', register.entities), ('holder', register.entities),
                              ('location', register.locations)):
            reference = getattr(fields, name)
            if reference is not None and reference.status == 'known' and reference.id not in catalog:
                errors.append(f'{label}.objects.{key}.{name}: 未知引用 {reference.id}')
        if snapshot and (any(getattr(fields, name).status != 'unknown' for name in ('owner', 'holder', 'location'))
                         or fields.condition is not None) and not fields.evidence:
            errors.append(f'{label}.objects.{key}: 已知初始状态须有原文依据')
    return errors


def asset_errors(world, register, chunks, initial=None, require_complete=False):
    """只查引用、证据和可回放结构；语义仍由原有独立检查完成。"""
    errors = []
    for event in world.events:
        if event.state_delta is None:
            if require_complete:
                errors.append(f'{event.id}.state_delta 尚未交付；无变化也需明确交空集合')
            continue
        errors.extend(state_reference_errors(event.state_delta, register, event.id + '.state_delta'))
        errors.extend(evidence_errors(event.state_delta, chunks))
        try:
            merge_deltas([event.state_delta])
        except ValueError as exc:
            errors.append(f'{event.id}: {exc}')
    if initial is not None:
        errors.extend(state_reference_errors(initial, register, 'initial_state'))
        errors.extend(evidence_errors(initial, chunks))
    elif require_complete:
        errors.append('information.initial_state尚未交付；不能用全文最终关系或物品位置代替故事初始状态')
    errors.extend(evidence_errors(list(register.objects.values()), chunks))
    for record in evidence_records([initial, list(register.objects.values()),
                                    [event.state_delta for event in world.events]]):
        if any(source.unverified_quote for source in record.evidence):
            errors.append('动态资产引文与来源不符；核对后只提交正确chunk/passage_ids')
    if require_complete:
        errors.extend(f'objects.{key}.evidence尚未交付' for key, row in register.objects.items() if not row.evidence)
    return errors


def initial_state_coverage(information, world):
    """区分模型明确交付的未知与漏填；不猜状态，也不枚举任意人物两两关系。"""
    if information is None or information.initial_state is None:
        return []  # 缺少整个initial_state由asset_errors报告。
    initial = information.initial_state
    errors = []
    missing = sorted(world.objects.keys() - initial.objects.keys())
    if missing:
        errors.append(f'initial_state.objects漏填{missing}；逐物品核对故事起点前状态，'
                      '确无依据也须明确填写unknown，不能靠导出默认值代表已提取')
    pairs = {(row.owner, row.other) for row in information.relations}
    pairs.update((row.owner_id, row.other_id) for event in getattr(world, 'events', [])
                 if event.state_delta is not None for row in event.state_delta.relations)
    supplied = {(row.owner_id, row.other_id) for row in initial.relations}
    missing_pairs = sorted(pairs - supplied)
    if missing_pairs:
        errors.append(f'initial_state.relations漏填已识别关系的端点{missing_pairs}；'
                      '核对各关系在起点的active/inactive/unknown，不把后来成立的关系前置')
    return errors


def apply_delta(state, delta):
    """一次应用同order组的合并delta；原状态不变，省略字段保持原值。"""
    value = state.model_dump(mode='json')
    relations = {relation_key(row): row.model_dump(mode='json') for row in state.relations}
    for row in delta.relations:
        key = relation_key(row)
        before = relations.get(key, {
            'owner_id': row.owner_id, 'other_id': row.other_id, 'relation': row.relation,
            'status': 'unknown', 'detail': None, 'evidence': [],
        })
        relations[key] = {**before, **row.set.model_dump(mode='json'),
                          'evidence': before['evidence'] + [e.model_dump(mode='json') for e in row.evidence]}
    value['relations'] = [relations[key] for key in sorted(relations)]
    for row in delta.objects:
        before = value['objects'].get(row.object_id, ObjectState().model_dump(mode='json'))
        value['objects'][row.object_id] = {**before, **row.set.model_dump(mode='json'),
            'evidence': before['evidence'] + [e.model_dump(mode='json') for e in row.evidence]}
    result = AssetState.model_validate(value)
    for record in evidence_records(result):
        record.evidence = unique_evidence(record.evidence)
    return result


def replay_states(world, initial, confirmed=None):
    """生成已确认前缀的节点入口快照；最后一组也必须实际回放并检查冲突。"""
    if initial is None:
        return {}, ['initial_state缺失，不能生成状态快照']
    if timeline_errors(world, require_complete=True):
        return {}, ['完整故事时间尚未就绪，不能猜测状态回放顺序']
    confirmed = {event.id for event in world.events} if confirmed is None else confirmed
    state, snapshots, previous = initial.model_copy(deep=True), {}, None
    for key in world.objects:
        state.objects.setdefault(key, ObjectState())
    for node in sorted(world.time_nodes, key=lambda node: node.order):
        events = sorted((event for event in world.events if event.time_node_id == node.id),
                        key=lambda event: (event.event_order, event.id))
        if any(event.id not in confirmed for event in events):
            return snapshots, [f'{node.id}时间尚未确认，后续状态快照停止生成']
        snapshots[node.id] = {'node_id': node.id, 'previous_node_id': previous, **public_state(state)}
        for order, members in groupby(events, key=lambda event: event.event_order):
            group = list(members)
            missing = [event.id for event in group if event.state_delta is None]
            if missing:
                return snapshots, [f'{node.id}/order={order} 缺少state_delta：{missing}']
            try:
                state = apply_delta(state, merge_deltas([event.state_delta for event in group]))
            except ValueError as exc:
                return snapshots, [f'{node.id}/order={order} 事件{[event.id for event in group]}：{exc}']
        previous = node.id
    return snapshots, []


def public_state(value, include_evidence=True):
    """投影副本，不改内部证据；旧格式可保留来源，新repository剔除全部evidence字段。"""
    excluded = {'unverified_quote'} | (set() if include_evidence else {'evidence'})
    def visit(item):
        if isinstance(item, dict):
            return {key: visit(row) for key, row in item.items() if key not in excluded}
        if isinstance(item, list):
            return [visit(row) for row in item]
        return item
    return visit(value.model_dump(mode='json') if hasattr(value, 'model_dump') else value)
