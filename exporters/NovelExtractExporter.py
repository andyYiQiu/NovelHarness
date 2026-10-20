"""把内部 World 投影到现有 repository；不让模型重写数据。"""
from collections import Counter, defaultdict
import ctypes
import errno
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

from common.novel_extract.NovelExtractRuntime import digest, encode, require, safe_path
from common.novel_extract.NovelExtractSchema import AssetState, RepositoryInformation, time_dependencies
from common.novel_extract.NovelExtractReader import Chunk
from common.novel_extract.NovelExtractState import (
    asset_errors, asset_references, initial_state_coverage, public_state, remap_assets, replay_states,
)


class NovelExtractExporter:
    def __init__(self, storage):
        self.storage = storage

    @staticmethod
    def filename(value):
        return re.sub(r'[^\w\-\u3400-\u9fff]+', '_', value).strip('_')[:70] or 'novel'

    @staticmethod
    def identifier(value):
        """保留正常内部编号，仅将不能作为路径的编号做确定性编码。"""
        return value if re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value) else 'ID_' + sha256(value.encode()).hexdigest()

    @staticmethod
    def publish(directory, target):
        """沿用既有的原子无覆盖发布；旧结果永不覆盖。"""
        libc = ctypes.CDLL(None, use_errno=True)
        source, destination = os.fsencode(directory), os.fsencode(target)
        if sys.platform == 'darwin':
            operation = libc.renamex_np
            operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            code = operation(source, destination, 4)
        elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
            operation = libc.renameat2
            operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            code = operation(-100, source, -100, destination, 1)
        else:
            raise OSError('当前平台不支持目录的原子无覆盖发布')
        if code:
            number = ctypes.get_errno()
            if number in (errno.EEXIST, errno.ENOTEMPTY):
                raise FileExistsError('输出已经存在，拒绝覆盖：' + str(target))
            raise OSError(number, os.strerror(number))

    @staticmethod
    def tree_hashes(path):
        result = {}
        for file in sorted(path.rglob('*')):
            require(not file.is_symlink(), '输出含符号链接，拒绝复用')
            if file.is_file():
                result[str(file.relative_to(path))] = sha256(file.read_bytes()).hexdigest()
        return result

    def check_existing(self, target):
        target = safe_path(target)
        incomplete = safe_path(str(target) + '.incomplete.json')
        receipt = self.storage.get('export')
        if not target.exists() and not incomplete.exists():
            return
        require(receipt and receipt['path'] == str(target)
                and self.storage.get('status') in ('complete', 'partial'), '输出已经存在；须指定新的输出目录')
        if target.exists():
            require(target.is_dir() and self.tree_hashes(target) == receipt['files'], '已有输出与收据不符，拒绝覆盖')
        if incomplete.exists():
            require(incomplete.is_file() and sha256(incomplete.read_bytes()).hexdigest() == receipt.get('incomplete_sha256'),
                    '已有未完成清单与收据不符，拒绝覆盖')

    def confirmed_event_ids(self, world):
        """核对工作流保存的依赖版本；导出不自行进行语义确认。"""
        confirmations = self.storage.get('review_state', {}).get('confirmed_times', {})
        return {key for key, value in time_dependencies(world).items() if confirmations.get(key) == digest(value)}

    def project(self, world):
        """只映射有直接对应的数据；未知字段留空，内部图始终完整保存在 SQLite。"""
        source = self.storage.get('source')
        source_file = self.filename(source)
        files, excluded = {}, []
        # 前置档案失败时全局阶段会显式保存 null 以保留真实状态；partial
        # 仍须导出已有事实，而不能因对 null 调用 get 导致未捕获异常。
        information = self.storage.latest('information') or {}
        roles = {key: self.identifier(key) for key in world.entities
                 if key in information.get('characters', {})}
        require(len(set(roles.values())) == len(roles), '人物文件编号冲突')
        relations = defaultdict(dict)
        for relation in information.get('relations', []):
            owner, other = relation['owner'], relation['other']
            if owner not in roles or other not in roles or owner == other:
                excluded.append({'kind': 'relation', 'reason': '最终图中的关系引用失效', 'record': relation})
                continue
            target = roles[other]
            current = relations[owner].setdefault(target, {'relation': [], 'detail': None})
            current['relation'] = list(dict.fromkeys(current['relation'] + relation['relation']))
            current['detail'] = '\n'.join(dict.fromkeys(filter(None, (current['detail'], relation['detail'])))) or None
        for key, entity in sorted(world.entities.items()):
            if key not in roles:
                excluded.append({'kind': 'character_information', 'reason': '人物资料未完成或更名后未对应',
                                 'record': entity.model_dump(mode='json')})
                continue
            code = roles[key]
            details = information['characters'][key]
            files[f'roles/{code}/role_info.json'] = {
                'role_code': code, 'role_name': entity.name, 'source': source, **details,
                'relation': relations.get(key) or None,
            }
        files[f'locations/{source_file}.json'] = {
            key: {'location_code': key, 'location_name': loc.name, 'source': source,
                  **information.get('locations', {}).get(key, {'description': loc.description or None, 'detail': None})}
            for key, loc in sorted(world.locations.items())}
        files['worlds/background/general.json'] = {
            'source': source, **information.get('background', {
                'world_name': None, 'language': None, 'description': None, 'detail': None})}
        files[f'worlds/settings/{source_file}.jsonl'] = information.get('settings', [])
        for group, live in (('characters', roles), ('locations', world.locations)):
            for key, value in information.get(group, {}).items():
                if key not in live:
                    excluded.append({'kind': group, 'id': key, 'reason': '内部图已移除该实体，旧资料仍保留', 'record': value})
        for key in set(world.locations) - set(information.get('locations', {})):
            excluded.append({'kind': 'location_information', 'id': key, 'reason': '地点资料尚未补全'})
        counts = Counter(e.id for e in world.events)
        groups = defaultdict(list)
        confirmed = self.confirmed_event_ids(world)
        node_members = defaultdict(set)
        for event in world.events:
            node_members[event.time_node_id].add(event.id)
        time_nodes = {node.id: node for node in world.time_nodes}
        node_details = {}
        for event in world.events:
            people = list(dict.fromkeys(event.actor_ids + event.target_ids))
            required_people = set(people)
            if self.storage.get('format', 37) >= 38:
                required_people.update(asset_references(event.state_delta)['entities'])
            missing = sorted(required_people - roles.keys())
            reason = ('事件编号重复' if counts[event.id] > 1 else
                      '事件正文为空' if not event.description.strip() else
                      '事件时间节点未交付；保留在未完成清单，不制造占位节点' if event.time_node_id not in time_nodes else
                      '事件人物档案尚未完成：' + ', '.join(missing) if missing else
                      '事件地点或物品引用不存在' if event.at_location_id
                      and event.at_location_id not in world.locations or set(event.object_ids) - world.objects.keys() else None)
            if reason:
                excluded.append({'kind': 'event', 'reason': reason, 'record': event.model_dump(mode='json')})
                continue
            # 整篇partial不抹掉独立时间确认；节点顺序须由全部成员的有效确认共同支持。
            node = time_nodes[event.time_node_id]
            group = self.identifier(node.id)
            node_details[group] = (node.order if node_members[node.id] <= confirmed else None, node.name, node.relative)
            groups[group].append({
                'task_id': event.id, 'order': event.event_order if event.id in confirmed else None,
                'todo': event.description + '\n依据性质：' + event.epistemic_status, 'time': event.source_time,
                'locations': [world.locations[event.at_location_id].name] if event.at_location_id else None,
                'participants': [{'role_code': roles[key], 'role_name': world.entities[key].name,
                                  'original_goal': event.goals.get(key)} for key in people],
                'items': [world.objects[key].name for key in event.object_ids] or None,
                'required_results': event.required_results,
            })
        nodes = []
        require(len({self.identifier(key) for key in time_nodes}) == len(time_nodes), '时间节点文件编号冲突')
        for node_id in sorted(groups, key=lambda key: (node_details[key][0] is None, node_details[key][0] or 0, key)):
            order, name, relative = node_details[node_id]
            path = f'{node_id}_{self.filename(name)}/todolist.json'
            tasks = sorted(groups[node_id], key=lambda row: (row['order'] is None, row['order'] or 0, row['task_id']))
            files['timeline/' + path] = {'node_id': node_id, 'todolist': tasks}
            nodes.append({'node_id': node_id, 'order': order, 'node_name': name,
                          'time': {'relative': relative,
                                   'exact_start': None, 'exact_end': None, 'duration': None},
                          'interval_from_previous': None, 'path': path})
        files['timeline/timeline.json'] = {'source': source, 'time_basis': 'story', 'nodes': nodes}
        if self.storage.get('format', 37) >= 38:
            self.project_dynamic_assets(world, information, roles, files, excluded, confirmed)
        if self.storage.get('format', 37) >= 39:
            files = public_state(files, include_evidence=False)
        return files, excluded

    def project_dynamic_assets(self, world, information, roles, files, excluded, confirmed):
        """格式38的物品档案、事件delta与节点入口快照；旧实验投影保持原样。"""
        source = self.storage.get('source')
        objects = {key: self.identifier(key) for key in world.objects}
        require(len(set(objects.values())) == len(objects), '物品文件编号冲突')
        redirects = {**roles, **objects}
        for code in roles.values():
            files[f'roles/{code}/role_info.json'].pop('relation', None)
        for key, item in world.objects.items():
            code = objects[key]
            files[f'objects/{code}/object_info.json'] = {
                'object_code': code, 'object_name': item.name, 'source': source,
                'aliases': item.aliases, 'description': item.description or None,
                'function': item.function, 'significance': item.significance,
                'evidence': public_state(item)['evidence'],
            }
        events = {event.id: event for event in world.events}
        exported_ids = set()
        for path, content in files.items():
            if path.startswith('timeline/') and path.endswith('/todolist.json'):
                for task in content['todolist']:
                    event = events[task['task_id']]
                    exported_ids.add(event.id)
                    task['object_ids'] = [objects[key] for key in event.object_ids]
                    task['state_delta'] = (public_state(remap_assets(event.state_delta, redirects))
                                           if event.state_delta is not None else None)
        try:
            details = RepositoryInformation.model_validate(information)
            initial = details.initial_state
            chunks = [Chunk(**row) for row in self.storage.latest('chunks', [])]
            errors = asset_errors(world, world, chunks, initial, require_complete=True)
            if self.storage.get('format', 37) >= 39:
                errors.extend(initial_state_coverage(details, world))
            missing_people = asset_references(initial)['entities'] - roles.keys()
            if missing_people:
                errors.append(f'初始状态引用的人物档案尚未导出：{sorted(missing_people)}')
            if exported_ids != events.keys():
                errors.append('部分事件未进入repository，不能发布依赖其delta的状态快照')
            if errors:
                snapshots = {}
            else:
                snapshots, errors = replay_states(world, initial, confirmed)
        except ValueError as exc:
            snapshots, errors = {}, [str(exc)]
        excluded.extend({'kind': 'dynamic_state', 'reason': message} for message in errors)
        snapshots = {self.identifier(key): value for key, value in snapshots.items()}
        for node in files['timeline/timeline.json']['nodes']:
            snapshot = snapshots.get(node['node_id'])
            node['state_path'] = None
            if snapshot is not None:
                node['state_path'] = str(Path(node['path']).with_name('state_before.json'))
                state = remap_assets(AssetState.model_validate(snapshot), redirects)
                previous = snapshot['previous_node_id']
                files['timeline/' + node['state_path']] = {
                    'node_id': node['node_id'],
                    'previous_node_id': self.identifier(previous) if previous else None,
                    **public_state(state),
                }

    def export(self, target, world):
        target = safe_path(target)
        self.check_existing(target)
        files, excluded = self.project(world)
        partial = self.storage.get('status') == 'partial'
        require(partial or self.storage.get('status') == 'complete', '尚未进入终态，不能发布正式输出')
        require(partial or self.storage.get('review_state', {}).get('accepted'), '完整输出必须先完成全局时间确认')
        require(partial or self.confirmed_event_ids(world) == {e.id for e in world.events},
                '完整输出的时间确认缺失或依赖已变化')
        require(partial or not excluded, '完整输出不能遗漏不能投影的资料')
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='.novel-export-', dir=target.parent) as temporary:
            stage = Path(temporary) / 'repository'
            for folder in ('roles', 'locations', 'worlds/background', 'worlds/settings', 'timeline'):
                (stage / folder).mkdir(parents=True, exist_ok=True)
            if self.storage.get('format', 37) >= 38:
                (stage / 'objects').mkdir()
            for relative, value in files.items():
                file = stage / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                content = ''.join(encode(row) + '\n' for row in value) if file.suffix == '.jsonl' else (
                    json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
                file.write_text(content, encoding='utf-8')
            receipt = {'path': str(target), 'files': self.tree_hashes(stage)}
            report_path = safe_path(str(target) + '.incomplete.json')
            if partial:
                report = {'result': 'partial' if world.entities or world.events else 'empty',
                          'source': self.storage.get('source'), 'workspace': str(self.storage.path),
                          'summary': '整篇尚未完整交付；非空顺序来自独立时间确认，仅针对当前已保存事件，不代表全文事实已通过。',
                          'termination': self.storage.get('termination'),
                          'validation': {'review': self.storage.latest('validation.review'),
                                         'state': self.storage.get('review_state', {})},
                          'failed_jobs': self.storage.latest('failures', []),
                          'chunks': self.storage.latest('chunks', []),
                          'not_exported_records': excluded,
                          'saved_events': [event.model_dump(mode='json') for event in world.events],
                          'saved_time_nodes': [node.model_dump(mode='json') for node in world.time_nodes],
                          'raw_data': '工作区 snapshots 保留内部图及所有阶段结果；calls 保留原始响应。'}
                staged_report = Path(temporary) / 'incomplete.json'
                staged_report.write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + '\n', encoding='utf-8')
                receipt['incomplete_sha256'] = sha256(staged_report.read_bytes()).hexdigest()
            previous = self.storage.get('export')
            require(not previous or previous == receipt, '恢复导出与原收据不同，拒绝改写')
            self.storage.set('export', receipt)
            if partial and not report_path.exists():
                self.publish(staged_report, report_path)
            if not target.exists():
                self.publish(stage, target)
        return receipt
