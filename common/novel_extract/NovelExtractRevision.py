"""小说资料修订：在副本上应用事实、时间和档案补丁，不负责调度或语义判断。"""
from copy import deepcopy

from common.novel_extract.NovelExtractRuntime import DeliveryStructureError
from common.novel_extract.NovelExtractState import merge_deltas, remap_assets
from common.novel_extract.NovelExtractSchema import (
    CharacterInformation, Correction, Entity, Event, Foundation, Location,
    LocationInformation, Object, Register, RepositoryInformation, Review,
    Topology, World, canonical_signature, compare_starts,
    event_time_content, information_errors, normalize_evidence,
    normalize_references, register_errors, temporal_errors, timeline_errors,
)


def _apply_catalog_correction(value, patch):
    """应用目录新增与合并，返回供全部下游引用统一使用的重定向。"""
    renames = {}
    for name, field in (
        ('entities', 'entity_merges'),
        ('locations', 'location_merges'),
        ('objects', 'object_merges'),
    ):
        rows, updates = getattr(value, name), deepcopy(getattr(patch, name))
        merges = getattr(patch, field)
        mapping = {merge.source: merge.target for merge in merges}
        if len(mapping) != len(merges) or set(mapping) & (set(mapping.values()) | set(updates)):
            raise ValueError(field + ': 不允许重复来源、合并链或同时改写来源')
        for source, target in mapping.items():
            if source not in rows or target not in rows or target not in updates:
                raise ValueError(field + ': 须引用两个已有对象，并完整提交保留对象')
        rows.update(updates)
        for source in mapping:
            del rows[source]
        renames.update(mapping)
    value.world_traits.update(patch.world_traits)
    return renames


def _revised_signature(signature, value, renames):
    if signature is None:
        return None
    revised = signature.model_copy(deep=True)
    revised.actual_subjects = [renames.get(key, key) for key in revised.actual_subjects]
    return canonical_signature(revised, value.entities)


def _collect_event_updates(value, review, pending, chunks, renames):
    """解析完整事件与字段补丁；这里只验证单个事件身份，不改变已存图。"""
    patch = review.correction
    events = {event.id: event for event in value.events}
    candidates = {event.id: event for event in pending.events}
    updates = {event.id: event.model_copy(deep=True) for event in patch.events}
    if len(updates) != len(patch.events):
        raise ValueError('correction.events 编号重复')
    for key, fields in patch.event_updates.items():
        original = updates.get(key) or events.get(key) or candidates.get(key)
        if original is None:
            raise ValueError(f'event_updates.{key}: 既不在已保存事件也不在待修候选中；新增事件须提交events完整内容')
        invalid = set(fields) - (Event.model_fields.keys() - {'id', 'time_node_id', 'event_order'})
        placement = next((p for p in review.time.timeline if p.id == key), None) if review.time else None
        fields = dict(fields)
        for name, target in (('time_node_id', 'node_id'), ('event_order', 'order')):
            if name == 'event_order' and isinstance(fields.get(name), str) and fields[name].isdecimal():
                fields[name] = int(fields[name])
            if (name in fields and not isinstance(fields[name], bool)
                    and placement is not None and fields[name] == getattr(placement, target)):
                fields.pop(name)
                invalid.discard(name)
        if invalid:
            raise ValueError(f'event_updates.{key} 非法字段 {sorted(invalid)}；时间改动使用time')
        if key in updates and any(fields[name] != updates[key].model_dump()[name] for name in fields):
            raise ValueError(f'event_updates.{key}: 与同次完整events提交有冲突，只提交一种明确修改')
        updates[key] = Event.model_validate({**original.model_dump(), **fields})
    for field, references in (
        ('entities', {key for event in updates.values() for key in event.actor_ids + event.target_ids}),
        ('locations', {event.at_location_id for event in updates.values() if event.at_location_id}),
        ('objects', {key for event in updates.values() for key in event.object_ids}),
    ):
        for key in references - getattr(value, field).keys():
            if key in getattr(pending, field):
                getattr(value, field)[key] = getattr(pending, field)[key].model_copy(deep=True)
    if chunks:
        normalize_evidence(updates.values(), chunks)
    normalize_references(Topology(events=list(updates.values())), value)
    for key, updated in updates.items():
        if key not in events:
            continue
        saved_signature = _revised_signature(events[key].occurrence_signature, value, renames)
        updated_signature = _revised_signature(updated.occurrence_signature, value, renames)
        if saved_signature is None or updated_signature != saved_signature:
            raise ValueError(f'event_updates.{key}: occurrence_signature是稳定发生身份，不能用同一编号替换另一件事；'
                             '新发生使用新event_id，旧事件按有证据删除协议处理')
    return events, updates


def _event_merge_plan(value, patch, events, updates, renames):
    """核验事件合并与删除，保留原有证据合并语义。"""
    mapping = {merge.source: merge.target for merge in patch.merges}
    removals = {removal.id for removal in patch.removals}
    if len(mapping) != len(patch.merges) or len(removals) != len(patch.removals):
        raise ValueError('合并或删除编号重复')
    if set(mapping) & (set(mapping.values()) | removals | set(updates)) or removals & set(updates):
        raise ValueError('合并链、同轮合并并改写来源或删除并更新存在歧义')
    time_repair = False
    for source, target in mapping.items():
        if source not in events or target not in events or target not in updates or source == target:
            raise ValueError('合并须引用两个已有事件，并完整提交保留事件：' + source)
        source_event, target_event = events[source], updates[target]
        source_signature = _revised_signature(source_event.occurrence_signature, value, renames)
        saved_target_signature = _revised_signature(events[target].occurrence_signature, value, renames)
        target_signature = _revised_signature(target_event.occurrence_signature, value, renames)
        if (source_signature is None or saved_target_signature is None or target_signature is None
                or source_signature.layer == '未分类'
                or not source_signature == saved_target_signature == target_signature):
            raise ValueError(f'事件合并 {source}→{target} 的四项发生签名不同或缺失；'
                             '不同实际主体、发生、开始阶段或层次不能合并')
        comparison = compare_starts(
            source_event, target_event, {node.id: node for node in value.time_nodes})
        if comparison not in (None, 0):
            time_repair = True
        evidence = [*updates[target].evidence, *events[source].evidence, *events[target].evidence]
        updates[target].evidence = list({
            (item.chunk, item.quote, tuple(item.passage_ids)): item for item in evidence
        }.values())
        explicit_delta = ('state_delta' in patch.event_updates.get(target, {})
                          or any(row.id == target and row.state_delta is not None for row in patch.events))
        target_delta = target_event.state_delta or events[target].state_delta
        if not explicit_delta and (source_event.state_delta is not None or target_delta is not None):
            updates[target].state_delta = merge_deltas([source_event.state_delta, target_delta])
    if not removals <= events.keys():
        raise ValueError('删除引用了不存在的事件')
    removal_rows = {row.id: row for row in patch.removals}
    for key in removals:
        removal, event = removal_rows[key], events[key]
        event_signature = _revised_signature(event.occurrence_signature, value, renames)
        removal_signature = (_revised_signature(removal.signature, value, renames)
                             if removal.signature is not None else event_signature)
        if (event_signature is None or event_signature.layer == '未分类'
                or removal_signature != event_signature):
            raise ValueError(f'事件删除 {key} 的发生签名与已保存事件不一致；不能删除')
    return mapping, removals, time_repair


def _commit_event_updates(events, updates, mapping, removals):
    """提交事件事实；事实变化仅撤销该事件的旧时间位置。"""
    for key, event in updates.items():
        if key in events:
            original = events[key]
            unchanged = event_time_content(event) == event_time_content(original)
            event.time_node_id = original.time_node_id if unchanged else None
            event.event_order = original.event_order if unchanged else None
        else:
            event.time_node_id, event.event_order = None, None
    events.update(deepcopy(updates))
    for key in set(mapping) | removals:
        del events[key]
    return events


def update_records(current, updates, removals, fields, references):
    """增量更新；错误删除给出当前记录，不猜测反向边或相似状态。"""
    def key(row):
        return tuple(getattr(row, field) for field in fields)

    rows = {key(row): deepcopy(row) for row in current}
    incoming = {key(row): deepcopy(row) for row in updates or []}
    removed = {key(row) for row in removals}
    # 同一目标重复删除只有一种结果；重复更新可能冲突，仍拒绝。
    if len(incoming) != len(updates or []):
        raise ValueError('关系/状态变化存在重复修改编号')
    errors = []
    for identity in sorted(removed - rows.keys()):
        labels = dict(zip(fields, identity))
        unknown = {field: labels[field] for field, ids in references.items()
                   if labels[field] not in ids}
        nearby = [row.model_dump(mode='json') for row_key, row in rows.items()
                  if (set(row_key[:2]) == set(identity[:2])
                      if fields[0] == 'source' else row_key[:2] == identity[:2])]
        if unknown or nearby:
            errors.append(f'{fields[0]} 删除目标 {labels} 不存在；未知引用 {unknown}；相关已保存记录 {nearby}。'
                          '对照当前记录修正删除目标；已无此记录则去掉冗余删除，不自动删除反向边或相似状态。')
    if errors:
        raise ValueError('\n'.join(errors))
    for identity in removed:
        rows.pop(identity, None)
    rows.update(incoming)
    return list(rows.values())


def _remap_graph(value, patch, events, renames, mapping, removals):
    """在目录/事件合并后统一重写图引用，并移除已经没有事件的节点。"""
    value.events = list(events.values())
    for event in value.events:
        event.state_delta = remap_assets(event.state_delta, renames)
        for field in ('actor_ids', 'target_ids', 'object_ids'):
            setattr(event, field, list(dict.fromkeys(
                renames.get(key, key) for key in getattr(event, field))))
        event.at_location_id = renames.get(event.at_location_id, event.at_location_id)
        if event.occurrence_signature is not None:
            event.occurrence_signature.actual_subjects = list(dict.fromkeys(
                renames.get(key, key) for key in event.occurrence_signature.actual_subjects))
        goals = {}
        for key, goal in event.goals.items():
            target = renames.get(key, key)
            goals[target] = '\n'.join(dict.fromkeys(filter(None, (goals.get(target), goal))))
        event.goals = goals
    for item in value.objects.values():
        item.location_id = renames.get(item.location_id, item.location_id)
    known_events = set(events) | set(mapping) | removals
    value.relations = update_records(
        value.relations, patch.relations, patch.relation_removals,
        ('source', 'target', 'kind'), {'source': known_events, 'target': known_events})
    value.changes = update_records(
        value.changes, patch.changes, patch.change_removals,
        ('event_id', 'subject_id', 'change'),
        {'event_id': known_events,
         'subject_id': set(value.entities) | set(value.locations) | set(value.objects)
                       | set(value.world_traits) | set(renames)})
    relations = []
    for relation in value.relations:
        old_source, old_target = relation.source, relation.target
        relation.source = mapping.get(old_source, old_source)
        relation.target = mapping.get(old_target, old_target)
        if old_source != old_target and relation.source == relation.target:
            continue
        relations.append(relation)
    value.relations = relations
    for change in value.changes:
        change.event_id = mapping.get(change.event_id, change.event_id)
        change.subject_id = renames.get(change.subject_id, change.subject_id)
    used = {event.time_node_id for event in value.events}
    value.time_nodes = [node for node in value.time_nodes if node.id in used]


def apply_correction(world, review, chunks=(), pending=None):
    """在副本上应用一份完整修订；拒绝歧义，旧快照由调用方保留。"""
    value = world.model_copy(deep=True)
    patch = review.correction
    pending = pending or World(entities={}, locations={}, objects={}, world_traits={})
    renames = _apply_catalog_correction(value, patch)
    if chunks:
        normalize_evidence(list(value.objects.values()), chunks)
    events, updates = _collect_event_updates(value, review, pending, chunks, renames)
    mapping, removals, time_repair = _event_merge_plan(value, patch, events, updates, renames)
    if time_repair and (review.time is None or not review.time.complete):
        raise ValueError('事件合并位于不同实际开始位置；须同轮提交合并后的完整time，合并与排时一起核验')
    events = _commit_event_updates(events, updates, mapping, removals)
    _remap_graph(value, patch, events, renames, mapping, removals)
    if time_repair:
        # 旧位置可被纠正，但不能先删除事件、再保存一个未通过检查的时间方案。
        value = apply_timeline(value, review.time, chunks, world.time_nodes)
        errors = timeline_errors(value, require_complete=True)
        if errors:
            raise ValueError('\n'.join(errors))
    return value


def apply_timeline(world, review, chunks, previous_nodes=()):
    """增量接收模型的两层位置；不按编号推理时间，最终完整性另行验收。"""
    value = world.model_copy(deep=True)
    ids = {event.id for event in value.events}
    placements, duplicates = {}, set()
    for item in review.timeline:
        if item.id in placements:
            duplicates.add(item.id)
        placements[item.id] = item
    errors = []
    nodes = {} if review.complete else {node.id: node for node in previous_nodes}
    if not review.complete:
        nodes.update({node.id: node for node in value.time_nodes})
    nodes.update({node.id: node for node in review.nodes})
    if len({node.id for node in review.nodes}) != len(review.nodes):
        errors.append('time.nodes 节点编号重复')
    for label, keys in (('多余', placements.keys() - ids), ('重复', duplicates)):
        if keys:
            errors.append(f'timeline {label}事件编号：{sorted(keys)}')
    if review.complete and ids - placements.keys():
        errors.append(f'完整分组缺少事件 {sorted(ids - placements.keys())}；每件事件必须恰好属于一个节点，不能删除困难事件')
    for event in value.events:
        if event.id in placements:
            placement = placements[event.id]
            if placement.node_id not in nodes:
                errors.append(f'{event.id} 引用未知时间节点 {placement.node_id}')
            event.time_node_id, event.event_order = placement.node_id, placement.order
            event.temporal_basis = placement.basis
    used = {event.time_node_id for event in value.events}
    value.time_nodes = [node.model_copy(deep=True) for key, node in nodes.items() if key in used]
    placed = value.model_copy(update={
        'events': [event for event in value.events if event.time_node_id is not None]})
    errors.extend(timeline_errors(placed) + temporal_errors(value))
    if errors:
        raise ValueError('\n'.join(errors))
    return value


def merge_bundle_gaps(correction, information):
    """返回每条目录合并及其缺少的保留对象目录/档案路径。"""
    information = information if isinstance(information, dict) else {}
    gaps = []
    specifications = (
        ('entity_merges', 'entities', Entity, 'characters', CharacterInformation),
        ('location_merges', 'locations', Location, 'locations', LocationInformation),
        ('object_merges', 'objects', Object, None, None),
    )
    for merge_field, catalog_field, catalog_schema, profile_field, profile_schema in specifications:
        catalog = getattr(correction, catalog_field)
        profiles = information.get(profile_field, {}) if profile_field else {}
        for merge in getattr(correction, merge_field):
            missing = []
            if merge.target not in catalog:
                missing.append(f'$.correction.{catalog_field}.{merge.target}')
            else:
                catalog_path = f'$.correction.{catalog_field}.{merge.target}'
                missing.extend(f'{catalog_path}.{field}' for field in catalog_schema.model_json_schema()['properties']
                               if field not in catalog[merge.target].model_fields_set)
            if profile_field:
                path = f'$.information.{profile_field}.{merge.target}'
                if not isinstance(profiles, dict) or merge.target not in profiles:
                    missing.append(path)
                else:
                    profile = profiles[merge.target]
                    if isinstance(profile, dict):
                        missing.extend(f'{path}.{field}' for field in profile_schema.model_fields
                                       if field not in profile)
                    else:
                        missing.append(path)
                    try:
                        profile_schema.model_validate(profile)
                    except ValueError:
                        if path not in missing:
                            missing.append(path + '(值或类型不符合档案协议)')
            if missing:
                gaps.append((merge_field, catalog_field, profile_field, merge, missing))
    return gaps


def merge_bundle_errors(correction, information):
    """一次列出整批合并包缺项，不让修复按 target 串行试错。"""
    return [f'合并交付结构错误：{field} {merge.source}→{merge.target} 缺少 '
            + '、'.join(missing)
            for field, _, _, merge, missing in merge_bundle_gaps(correction, information)]


def apply_information(current, review, register, complete=True, chunks=()):
    """按对象增量修改档案，并同步显式实体合并。"""
    bundle_errors = merge_bundle_errors(review.correction, review.information)
    if bundle_errors:
        raise DeliveryStructureError('\n'.join(bundle_errors) +
            '\n请在同一份修订中一次交齐上述保留对象；未接收前source目录、档案及引用均保持不变。')
    value = deepcopy(current)
    patch = review.information or {}
    for field, update in patch.items():
        if field in ('characters', 'locations'):
            if not isinstance(update, dict):
                raise ValueError(f'information.{field} 须为编号到档案的对象')
            previous = value.get(field, {})
            value[field] = dict(previous if isinstance(previous, dict) else {})
            for key, profile in update.items():
                if not isinstance(profile, dict):
                    raise ValueError(f'information.{field}.{key} 须为档案对象')
                old = value[field].get(key, {})
                value[field][key] = {**(old if isinstance(old, dict) else {}), **profile}
        else:
            value[field] = update
    for field, merges in (('characters', review.correction.entity_merges),
                          ('locations', review.correction.location_merges)):
        for merge in merges:
            if merge.target not in patch.get(field, {}):
                raise ValueError(f'information.{field} 须提供合并后的完整档案 {merge.target}')
            value.get(field, {}).pop(merge.source, None)
    renames = {merge.source: merge.target for merge in review.correction.entity_merges}
    relations = []
    rows = value.get('relations') or []
    if not isinstance(rows, list):
        raise ValueError('information.relations 须为关系列表')
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(key), str)
                                            for key in ('owner', 'other')):
            raise ValueError('information.relations 每项须包含owner/other人物编号')
        old_pair = row['owner'], row['other']
        row['owner'], row['other'] = (renames.get(key, key) for key in old_pair)
        if old_pair[0] != old_pair[1] and row['owner'] == row['other']:
            continue
        relations.append(row)
    value['relations'] = relations
    for chain in value.get('story_spine', {}).get('causal_chains', []):
        for link in chain:
            signature = link.get('signature') if isinstance(link, dict) else None
            if isinstance(signature, dict):
                signature['actual_subjects'] = list(dict.fromkeys(
                    renames.get(key, key) for key in signature.get('actual_subjects', [])))
    details = RepositoryInformation.model_validate(value)
    asset_renames = {merge.source: merge.target for field in
                    ('entity_merges', 'location_merges', 'object_merges')
                    for merge in getattr(review.correction, field)}
    details.initial_state = remap_assets(details.initial_state, asset_renames)
    if chunks:
        normalize_evidence(details.initial_state, chunks)
    errors = information_errors(details, register, complete=complete)
    if errors:
        raise ValueError('\n'.join(errors))
    return details


def apply_foundation_review(foundation, check, chunks=()):
    """复用目录和档案修订协议处理全文基础资料核对。"""
    review = Review(
        is_valid=False, summary=check.summary, issues=check.issues, covered_chunks=[],
        correction=Correction.model_validate(check.correction.model_dump(exclude_unset=True)),
        information=(check.information.model_dump(mode='json')
                     if isinstance(check.information, RepositoryInformation)
                     else check.information),
    )
    bundle_errors = merge_bundle_errors(review.correction, review.information)
    if bundle_errors:
        raise DeliveryStructureError('\n'.join(bundle_errors) +
            '\n请在同一份修订中一次交齐上述保留对象；未接收前source目录、档案及引用均保持不变。')
    world = apply_correction(World(**foundation.catalog.model_dump()), review, chunks)
    errors = register_errors(world)
    if errors:
        raise ValueError('\n'.join(errors))
    information = apply_information(foundation.information, review, world, complete=False, chunks=chunks)
    return Foundation(
        catalog=Register.model_validate(world.model_dump()),
        information=information.model_dump(mode='json'),
    )
