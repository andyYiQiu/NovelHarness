"""小说提取工作流：入口、固定阶段依赖、跨块核实、时间确认及原子收尾。"""
import argparse
import asyncio
from copy import deepcopy
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import time

from pydantic import BaseModel, Field, ValidationError

from common.novel_extract.NovelExtractRuntime import ROOT, JobFailed, RunStopped, digest, fingerprint, require, safe_path
from storage.novel_extract.NovelExtractStorage import NovelExtractStorage
from common.novel_extract.NovelExtractReader import chunk_text, compact_evidence, full_source_context
from common.novel_extract.NovelExtractSchema import (
    Consequences, Correction, EventPass, Foundation, FoundationReview, FoundationFullReview, InformationPatch, Reading, Register, Review,
    World, RepositoryInformation, Timeline, TimelinePlan, information_errors,
    canonical_keys, evidence_errors, normalize_evidence, normalize_references, register_errors,
    fact_dependencies, time_dependencies, timeline_errors, temporal_errors, topology_errors,
)
from common.novel_extract.NovelExtractRevision import (
    apply_correction, apply_foundation_review, apply_information, apply_timeline,
    merge_bundle_errors, merge_bundle_gaps,
)


class ReviewState(BaseModel):
    """工作流拥有阶段状态；与该阶段资料修订一起原子保存。"""
    rounds: int = 0
    time_reviewed: bool = False
    accepted: bool = False
    confirmed_times: dict[str, str] = Field(default_factory=dict)
    time_placements: list[str] = Field(default_factory=list)
    signatures: list[str] = Field(default_factory=list)
    resolved_jobs: list[str] = Field(default_factory=list)
    history: list[dict] = Field(default_factory=list)
    next_task: str = 'timeline'
    feedback: dict[str, dict] = Field(default_factory=dict)
    checked_facts: dict[int, str] = Field(default_factory=dict)
    entity_redirects: dict[str, str] = Field(default_factory=dict)


def prompt(name):
    return (ROOT / 'prompts/novel_extract' / name).read_text(encoding='utf-8')


def saved_world(storage):
    value = storage.latest('world')
    if value is not None:
        return World.model_validate(value)
    return World.model_validate({**storage.latest('register', {
        'entities': {}, 'locations': {}, 'objects': {}, 'world_traits': {}}), 'events': []})


def substantive_world(world):
    """比较会影响事实或开始顺序的内容，忽略纯说明文字。"""
    data = world.model_dump(mode='json')
    for event in data['events']:
        event.pop('temporal_basis')
    data['time_nodes'] = [{key: node[key] for key in ('id', 'order')}
                          for node in data['time_nodes']]
    return data


class NovelExtractWorkflow:
    def arguments(self, argv=None):
        parser = argparse.ArgumentParser(description='AutoGen：全文目录及档案 → 分块 → 核心事件抽取 → 结构修复 → 跨块/时间校验 → repository')
        parser.add_argument('--input', type=Path)
        parser.add_argument('--config', type=Path, default=Path('model/config.local.json'))
        parser.add_argument('--workspace', type=Path, required=True)
        parser.add_argument('--output', type=Path)
        parser.add_argument('--log-dir', type=Path)
        parser.add_argument('--max-steps', type=int, default=1000, help='累计 HTTP 请求上限，含重试；同时受配置 max_calls 约束')
        parser.add_argument('--max-request-chars', type=int, default=0, help='完整请求字符限制；0 不另加限制，接口仍有上下文限制')
        parser.add_argument('--concurrency', type=int, default=16, help='最大并行文本块数；不并行同块依赖步骤')
        parser.add_argument('--log-terminal', choices=('full', 'summary', 'quiet'), default='summary')
        parser.add_argument('--status', action='store_true', help='只读工作区，不加载模型或读取密钥')
        parser.add_argument('--preflight', action='store_true', help='离线检查依赖、配置、路径与分块；不建实验、不调用模型')
        return parser.parse_args(argv)

    def run(self, argv=None):
        args = self.arguments(argv)
        try:
            if args.status:
                print(json.dumps(NovelExtractStorage.read_status(args.workspace), ensure_ascii=False, indent=2))
                return 0
            return self.execute(args)
        except KeyboardInterrupt:
            print('已中断。工作区及收到的响应保留；同一命令可续跑。')
            return 130
        except RunStopped as exc:
            print('已暂停：' + str(exc) + '。工作区保持 working；条件恢复后使用同一命令续跑。')
            return 75
        except (ValueError, OSError) as exc:
            print('未完成：' + str(exc))
            return 1

    def execute(self, args):
        from agents.NovelExtractAgent import NovelExtractAgent
        from exporters.NovelExtractExporter import NovelExtractExporter
        from model.ModelClient import ModelClient
        from storage.novel_extract.NovelExtractLog import NovelExtractLog
        require(args.input and args.output and args.log_dir, '运行和预检均需 --input、--output、--log-dir')
        require(args.max_steps > 0 and args.max_request_chars >= 0 and args.concurrency > 0, '调用/请求/并行限制非法')
        require(version('autogen-agentchat') == '0.7.5' and version('autogen-core') == '0.7.5', '请按 requirements.txt 安装 AutoGen 0.7.5')
        source_path = safe_path(args.input)
        text = source_path.read_text(encoding='utf-8')
        require(bool(text.strip()), '输入原文为空')
        config = ModelClient.read_config(safe_path(args.config))
        output, log_dir, workspace = safe_path(args.output), safe_path(args.log_dir), safe_path(args.workspace)
        # 输出发布与日志/数据库不得互相包含，更不能覆盖输入。
        require(not output.exists() or output.is_dir(), '输出路径不是目录')
        paths = (output, log_dir, workspace)
        require(all(left != right and left not in right.parents and right not in left.parents
                    for index, left in enumerate(paths) for right in paths[index + 1:]), '输出、日志与工作区路径不能重叠')
        require(all(path != source_path and path not in source_path.parents for path in paths), '产物路径不能覆盖输入')
        policy = {'config': config, 'concurrency': args.concurrency, 'chunk_min_chars': 800, 'chunk_max_chars': 1200,
                  'context_tail_chars': 300, 'output_repairs': 5, 'validation_repairs': 2, 'review_rounds': 6,
                  'max_calls': min(args.max_steps, config['max_calls']), 'max_request_chars': args.max_request_chars}
        identity = {'format': NovelExtractStorage.FORMAT_VERSION, 'source': source_path.stem,
                    'source_sha256': sha256(text.encode('utf-8')).hexdigest(), 'fingerprint': fingerprint(),
                    'output': str(output), 'log_dir': str(log_dir), 'policy': policy}
        if args.preflight:
            if workspace.exists():
                previous = NovelExtractStorage.read_status(workspace)
                require(all(previous.get(k) == v for k, v in identity.items()), '预检失败：工作区与本次输入、版本、配置或路径不匹配')
            else:
                require(not output.exists() and not Path(str(output) + '.incomplete.json').exists(), '预检失败：新实验不能覆盖已有输出')
                require(not log_dir.exists() or not any(log_dir.iterdir()), '预检失败：新实验日志目录须为空')
            chunks = chunk_text(text)
            print(json.dumps({'preflight': 'passed', 'framework': 'AutoGen 0.7.5', 'source_chars': len(text),
                              'chunks': len(chunks), 'max_chunk_chars': max(map(lambda c: len(c.text), chunks)),
                              'fingerprint': identity['fingerprint'], 'policy': policy,
                              'limitations': '仅本地预检；未验证真实接口、抽取质量或速度，不会自动开始实验。'}, ensure_ascii=False, indent=2))
            return 0
        has_api_key = bool(os.environ.get(config['api_key_env'], ''))
        if not workspace.exists() and not has_api_key:
            raise RunStopped('未设置配置指定的密钥环境变量 ' + config['api_key_env'])
        storage = NovelExtractStorage(workspace)
        log = None
        try:
            if storage.get('format'):
                require(all(storage.get(k) == v for k, v in identity.items()), '工作区输入、版本、配置或路径已变化；不能混用实验')
            else:
                require(not output.exists() and not Path(str(output) + '.incomplete.json').exists(), '输出已存在，请使用新路径')
                require(not log_dir.exists() or not any(log_dir.iterdir()), '新实验日志目录须为空，不能混入历史日志')
                with storage.transaction():
                    for key, value in identity.items():
                        storage.set(key, value)
                    storage.set('status', 'working')
                    storage.set('started_at', time.time())
                    storage.set('input_text', text)
            exporter = NovelExtractExporter(storage)
            exporter.check_existing(output)
            if storage.get('status') in ('complete', 'partial'):
                exporter.export(output, saved_world(storage))
                print('终态输出已核对/恢复，零模型调用：' + storage.get('status'))
                return 0 if storage.get('status') == 'complete' else 2
            if not has_api_key:
                raise RunStopped('未设置配置指定的密钥环境变量 ' + config['api_key_env'])
            log = NovelExtractLog(log_dir, args.log_terminal)
            client = ModelClient(config, storage, log, args.max_steps, args.max_request_chars)
            self.storage, self.log, self.concurrency = storage, log, args.concurrency
            self.review_rounds = policy['review_rounds']
            self.agent = NovelExtractAgent(storage, client, log,
                repairs=policy['output_repairs'], validation_repairs=policy['validation_repairs'])
            self.failures = []
            try:
                world, accepted = asyncio.run(self.extract(text))
            except Exception as exc:
                # 未分类程序错误与存储错误不伪装成合法 partial。
                storage.set('last_error', {'type': type(exc).__name__, 'detail': str(exc)})
                log.write('error', error=type(exc).__name__, detail=str(exc))
                raise
            reasons = []
            if not accepted:
                reasons.append('跨块资料与时间校验尚未完整通过')
            if self.failures:
                reasons.append('存在未完整交付的任务；候选、有效部分及反馈已保留')
            if storage.latest('information') is None:
                reasons.append('人物、地点、背景资料尚未完整交付')
            _, excluded = exporter.project(world)
            if excluded:
                reasons.append('部分资料无法满足 repository 引用结构，已列入未完成清单')
            with storage.transaction():
                storage.snapshot('world', world.model_dump(mode='json'))
                storage.set('termination', {'reasons': reasons})
                storage.set('status', 'partial' if reasons else 'complete')
                storage.set('finished_at', time.time())
            exporter.export(output, world)
            log.write('finished', status=storage.get('status'), events=len(world.events), reasons=reasons,
                      elapsed_seconds=round(time.time() - storage.get('started_at'), 3))
            return 2 if reasons else 0
        finally:
            if log:
                log.close()
            storage.close()

    def stage(self, name, **data):
        self.storage.set('stage', name)
        self.log.write('stage', stage=name, **data)

    def failed(self, job, error):
        record = {'job': job, 'error': str(error)}
        self.failures.append(record)
        self.storage.snapshot('failures', self.failures)
        self.log.write('error', **record)

    @staticmethod
    def recover_foundation_fields(initial, submitted):
        """保留失败核对中的有效修订；无效字段退回原值并保持待核状态。"""
        check = FoundationReview.model_validate(submitted)
        patch = deepcopy(check.information or {})
        try:
            return check, apply_foundation_review(initial, check), []
        except ValidationError as exc:
            rejected = []
            for issue in exc.errors():
                path = issue['loc']
                if len(path) != 3 or path[0] not in ('characters', 'locations'):
                    raise
                section, key, field = path
                old = initial.information.get(section, {}).get(key, {})
                changed = patch.get(section, {}).get(key, {})
                if not isinstance(old, dict) or field not in old or not isinstance(changed, dict) or field not in changed:
                    raise
                changed.pop(field)
                rejected.append('.'.join(map(str, path)))
            check.information = patch
            return check, apply_foundation_review(initial, check), rejected

    async def foundation(self, text):
        """一次建立全文目录及档案，再独立增量核对；已有效的目录不因档案缺项停用。"""
        if self.storage.get('foundation_complete'):
            self.failures = self.storage.latest('failures', [])
            return Register.model_validate(self.storage.latest('register'))
        self.stage('全文人物、地点、物品及世界背景提取')
        instructions = prompt('NovelInformationPrompt.md')
        try:
            initial = await self.agent.run('foundation.extract', 'foundation_extractor', instructions,
                {'responsibility': 'extract', 'novel_source': text,
                 'information_schema': RepositoryInformation.model_json_schema()}, Foundation,
                lambda value: register_errors(value.catalog))
        except JobFailed as exc:
            self.failed('foundation.extract', exc)
            # 目录任务的模型交付失败不能变成整篇停工。空目录只是合法的
            # 分块起点，不表示资料已完成：各块仍须从原文补建对象并通过引用校验，
            # foundation.extract 失败职责保留给全局 Review 补齐，因此收尾仍为 partial。
            fallback = Register(entities={}, locations={}, objects={}, world_traits={})
            with self.storage.transaction():
                self.storage.snapshot('register', fallback.model_dump(mode='json'))
                self.storage.set('foundation_complete', True)
            return fallback
        except RunStopped as exc:
            # 预算、凭据或运行政策停止后无法继续调用后续角色，不把它伪装成
            # 可隔离的单任务质量失败。
            self.log.write('error', job='foundation.extract', error=str(exc))
            raise
        # 原始交付和目录先保存；档案错误留给独立核对，不把全部实体反复重提。
        with self.storage.transaction():
            self.storage.snapshot('foundation.initial', initial.model_dump(mode='json'))
            self.storage.snapshot('register', initial.catalog.model_dump(mode='json'))
        issues = []
        needs_full_information = False
        try:
            details = RepositoryInformation.model_validate(initial.information)
            issues = information_errors(details, initial.catalog)
        except ValueError as exc:
            issues = [str(exc)]
            needs_full_information = True
        result = initial
        check = None
        rejected_fields = []
        try:
            def validate(check):
                # 同一请求内只修复无法落库的交付结构。资料是否完整、准确以及
                # audit 中主动报告的疑点，要在保存候选后作为语义结果处理；
                # 否则紧凑格式修复既看不到原文，又会把正常发现的问题误当失败。
                revised = apply_foundation_review(initial, check)
                RepositoryInformation.model_validate(revised.information)
                return []
            check = await self.agent.run('foundation.check', 'foundation_checker', instructions,
                {'responsibility': 'check', 'novel_source': text, 'current': initial.model_dump(mode='json'),
                 'structural_errors': issues, 'information_schema': RepositoryInformation.model_json_schema()},
                FoundationFullReview if needs_full_information else FoundationReview, validate)
            result = apply_foundation_review(initial, check)
            if check.issues:
                self.failed('foundation.check', '; '.join(check.issues))
        except RunStopped:
            raise
        except JobFailed as exc:
            self.failed('foundation.check', exc)
            submitted = self.storage.latest('job:foundation.check', {}).get('submitted_candidate')
            if submitted:
                try:
                    check, result, rejected_fields = self.recover_foundation_fields(initial, submitted)
                except ValueError:
                    pass
        details = None
        try:
            candidate = RepositoryInformation.model_validate(result.information)
            details = candidate
            invalid = information_errors(candidate, result.catalog, complete=True)
            if invalid:
                self.failed('foundation.information', '\n'.join(dict.fromkeys(invalid)))
            if rejected_fields:
                self.failed('foundation.information',
                            '核对回复中的无效档案字段待复核：' + ', '.join(rejected_fields))
        except ValueError as exc:
            self.failed('foundation.information', exc)
        # 固定交接点保证恢复不覆盖核对后的目录、漏报失败或重复提取全文。
        with self.storage.transaction():
            self.storage.snapshot('register', result.catalog.model_dump(mode='json'))
            self.storage.snapshot('foundation.result', result.model_dump(mode='json'))
            if details is not None:
                self.storage.snapshot('information', details.model_dump(mode='json'))
            self.storage.set('foundation_complete', True)
        return result.catalog

    async def extract_chunk(self, chunk, chunks, register, information, semaphore):
        """各块独立并行；本块阅读、两遍事件工作和后果提取严格交接。"""
        async with semaphore:
            topology = World(**register.model_dump())
            base = {'chunk': chunk.context(chunks), 'register': register.model_dump(mode='json'),
                    'full_story_overview': information.story_overview if information else None,
                    'story_reference': (information.model_dump(mode='json', exclude_none=True,
                        exclude={'story_overview'}) if information else None)}
            instructions = prompt('NovelExtractionPrompt.md')
            prefix = f'chunk.{chunk.index}'
            steps = ('reading', 'first_pass', 'second_pass', 'consequences')
            job = prefix + '.reading'
            try:
                def validate_reading(value):
                    normalize_evidence(value.core_candidates, chunks)
                    errors = evidence_errors(value.core_candidates, chunks) + [
                        '阅读候选只能引用本块原文' for candidate in value.core_candidates
                        if any(q.chunk != chunk.index for q in candidate.evidence)]
                    errors.extend(f'核心候选{index}缺少明确实际主体' for index, candidate in enumerate(value.core_candidates, 1)
                                  if not candidate.signature.actual_subjects)
                    errors.extend(f'核心候选{index}须明确发生层次，不能使用未分类' for index, candidate in enumerate(value.core_candidates, 1)
                                  if candidate.signature.layer == '未分类')
                    return errors
                reading = await self.agent.run(job, 'reader', instructions,
                    {**base, 'responsibility': 'reading'}, Reading, validate_reading)
                for phase in ('first_pass', 'second_pass'):
                    job = prefix + '.' + phase
                    prior = topology.model_copy(deep=True)
                    def validate(value):
                        existing = {event.id: event for event in prior.events}
                        # 旧事件重发只作为核实建议，不让建议中的复抄错误阻断本轮新增事实。
                        normalize_evidence([e for e in value.events if e.id not in existing], chunks)
                        errors = []
                        for field, namespace in (('entities', 'ENT'), ('locations', 'LOC'), ('objects', 'OBJ')):
                            for key, item in getattr(value, field).items():
                                if key in getattr(prior, field):
                                    if item != getattr(prior, field)[key]:
                                        errors.append(f'{field}.{key} 已有对象不能在块内改写；只补建漏项，纠错写coverage')
                                elif not key.startswith(f'{namespace}_C{chunk.index}_'):
                                    errors.append(f'{field}.{key} 新对象使用 {namespace}_C{chunk.index}_ 前缀')
                        for node in value.time_nodes:
                            if not node.id.startswith(f'TN_C{chunk.index}_'):
                                errors.append('块内时间节点使用 TN_C' + str(chunk.index) + '_ 前缀：' + node.id)
                        for event in value.events:
                            if event.id not in existing and not event.id.startswith(f'EVT_C{chunk.index}_'):
                                errors.append(f'新事件编号须使用 EVT_C{chunk.index}_ 前缀：' + event.id)
                            if event.id not in existing and any(q.chunk not in (chunk.index, chunk.index - 1) or
                                   q.chunk == chunk.index - 1 and q.quote not in chunk.previous_context
                                   for q in event.evidence):
                                errors.append('本块只能引用当前块及已展示的前块原文：' + event.id)
                            if event.id not in existing and event.occurrence_layer == '未分类':
                                errors.append(f'{event.id}须明确occurrence_layer，不能以历史兼容值未分类交付')
                        # 重发旧事件不会覆盖已交付事实；变化作为建议交给独立核实。
                        additions = value.model_copy(update={
                            'events': [event for event in value.events if event.id not in existing]})
                        combined = self.combine(prior, additions)
                        normalize_references(value, combined)
                        if phase == 'second_pass':
                            additions, _ = self.second_pass_update(prior, value)
                        combined = self.combine(prior, additions)
                        return errors + register_errors(combined) + timeline_errors(combined) + topology_errors(
                            combined, combined, chunks, check_time=True)
                    result = await self.agent.run(job, 'event_extractor', instructions,
                        {**base, 'register': Register.model_validate(prior.model_dump()).model_dump(mode='json'),
                         'responsibility': phase, 'reading': reading.model_dump(mode='json'),
                         'current': prior.model_dump(mode='json')}, EventPass, validate)
                    existing = {event.id: event for event in topology.events}
                    if phase == 'second_pass':
                        result, proposals = self.second_pass_update(topology, result)
                        self.storage.snapshot(prefix + '.proposals', proposals)
                    else:
                        result = result.model_copy(update={
                            'events': [event for event in result.events if event.id not in existing]})
                    topology = self.combine(topology, result)
                    self.storage.snapshot(prefix + '.topology', topology.model_dump(mode='json'))
                job = prefix + '.consequences'
                result = await self.agent.run(job, 'consequences', instructions,
                    {**base, 'register': Register.model_validate(topology.model_dump()).model_dump(mode='json'),
                     'responsibility': 'consequences', 'current': topology.model_dump(mode='json')},
                    Consequences, lambda value: topology_errors(self.combine(topology, value), topology, chunks, check_time=True))
                topology = self.combine(topology, result)
                self.storage.snapshot(prefix + '.topology', topology.model_dump(mode='json'))
            except RunStopped:
                raise
            except JobFailed as exc:
                self.failed(job, exc)
                for step in steps[steps.index(job.rsplit('.', 1)[-1]) + 1:]:
                    self.failed(prefix + '.' + step, '前置任务未交付：' + job)
            return topology

    @staticmethod
    def separate_second_pass(prior, incoming):
        """同一发生的第二遍交付留作核对建议，不另建重复事件。"""
        def anchors(event):
            return {(item.chunk, value) for item in event.evidence
                    for value in [*item.passage_ids, item.quote] if value}

        by_id = {event.id: event for event in prior}
        by_signature = {}
        for event in prior:
            if event.occurrence_signature is not None:
                key = digest(event.occurrence_signature.model_dump(mode='json'))
                by_signature[key] = event if key not in by_signature else None
        additions, proposals = [], []
        for event in incoming:
            original = by_id.get(event.id)
            if original is None and event.occurrence_signature is not None:
                original = by_signature.get(digest(event.occurrence_signature.model_dump(mode='json')))
                if original is not None and not anchors(original) & anchors(event):
                    original = None
            if original is None:
                additions.append(event)
            elif event.model_dump(exclude={'id'}) != original.model_dump(exclude={'id'}):
                proposals.append({'original': original.model_dump(mode='json'),
                                  'proposed': event.model_dump(mode='json')})
        return additions, proposals

    @classmethod
    def second_pass_update(cls, prior, result):
        """保留旧事件事实，仅接收同身份旧事件的局部时间修正。"""
        additions, proposals = cls.separate_second_pass(prior.events, result.events)
        existing = {event.id: event for event in prior.events}
        for event in result.events:
            old = existing.get(event.id)
            if (old is not None and old.occurrence_signature is not None
                    and old.occurrence_signature == event.occurrence_signature
                    and event.time_node_id is not None and event.event_order is not None
                    and (old.time_node_id, old.event_order) != (event.time_node_id, event.event_order)):
                additions.append(old.model_copy(update={
                    'time_node_id': event.time_node_id, 'event_order': event.event_order}))
        return result.model_copy(update={'events': additions}), proposals

    @staticmethod
    def combine(prior, update):
        """只按明确编号装配，逐字去重边和变化，不按相似度合并事实。"""
        events = {event.id: event for event in prior.events}
        incoming = getattr(update, 'events', [])
        if len({event.id for event in incoming}) != len(incoming):
            raise ValueError('同一提交中存在重复事件编号')
        events.update({event.id: event for event in incoming})
        def distinct(rows):
            return list({digest(row.model_dump(mode='json')): row for row in rows}.values())
        nodes = {node.id: node for node in prior.time_nodes}
        incoming_nodes = getattr(update, 'time_nodes', [])
        if len({node.id for node in incoming_nodes}) != len(incoming_nodes):
            raise ValueError('同一提交中存在重复时间节点编号')
        nodes.update({node.id: node for node in incoming_nodes})
        value = prior.model_copy(deep=True, update={'events': list(events.values()), 'time_nodes': list(nodes.values()),
            'relations': distinct(prior.relations + getattr(update, 'relations', [])),
            'changes': distinct(prior.changes + getattr(update, 'changes', []))})
        for field in ('entities', 'locations', 'objects'):
            getattr(value, field).update(getattr(update, field, {}))
        return value

    def pending_world(self):
        """待修候选不是已保存事实；只在检查明确选择其编号时补建并重新校验。"""
        pending = World(entities={}, locations={}, objects={}, world_traits={})
        for failure in self.failures:
            if not failure['job'].endswith(('.first_pass', '.second_pass')):
                continue
            saved = self.storage.latest('job:' + failure['job'], {})
            try:
                data = saved.get('candidate') or json.loads(saved.get('response', ''))
                candidate = EventPass.model_validate(data)
            except ValueError:
                continue
            pending = self.combine(pending, candidate)
        return pending

    async def supplement_information(self, chunks, world):
        """补建实体后的档案交接；只请求缺项，不让导出阶段才发现参与者无档案。"""
        current = self.storage.latest('information')
        if not current:
            return  # 联合提取的档案失败时由统一检查补齐，不能凭程序填造背景。
        missing = {field: {key: value.model_dump(mode='json') for key, value in rows.items()
                           if key not in current.get(field, {})}
                   for field, rows in (('characters', world.entities), ('locations', world.locations))}
        if not any(missing.values()):
            return
        job = 'information.supplement.' + digest(missing)[:16]
        def validate(value):
            errors = []
            for field, register in (('characters', world.entities), ('locations', world.locations)):
                rows = canonical_keys(getattr(value, field), register)
                setattr(value, field, rows)
                if set(rows) != set(missing[field]):
                    errors.append(f'{field} 仅补missing中的编号：缺少 {sorted(missing[field].keys() - rows.keys())}；'
                                  f'越界 {sorted(rows.keys() - missing[field].keys())}')
            if errors:
                return errors
            review = Review(is_valid=False, summary='补档', issues=[], covered_chunks=[],
                            information=value.model_dump(mode='json'))
            apply_information(current, review, world)
            return []
        try:
            result = await self.agent.run(job, 'information', prompt('NovelInformationPrompt.md'),
                {'responsibility': '仅补齐missing中的人物和地点档案，保留身份区别，不重写其他资料',
                 'missing': missing, 'source_chunks': [c.context() for c in chunks]}, InformationPatch, validate)
        except RunStopped:
            raise
        except JobFailed as exc:
            if not any(f['job'] == job for f in self.failures):
                self.failed(job, exc)
            return
        review = Review(is_valid=False, summary='补档', issues=[], covered_chunks=[],
                        information=result.model_dump(mode='json'))
        self.storage.snapshot('information', apply_information(current, review, world).model_dump(mode='json'))

    def review_context(self, chunks, world):
        """全文语义审计：给原文、候选和结果，只要求报告具体错误。"""
        information = self.storage.latest('information')
        draft = self.storage.latest('foundation.result', {}).get('information')
        state = ReviewState.model_validate(self.storage.get('review_state', {}))
        checked = {index for index, data in fact_dependencies(world, chunks).items()
                   if state.checked_facts.get(index) == digest(data)}
        open_chunks = {chunk.index for chunk in chunks} - checked
        diagnostics = register_errors(world) + topology_errors(world, world, chunks, check_time=True)
        if information or draft:
            try:
                diagnostics += information_errors(
                    RepositoryInformation.model_validate(information or draft), world)
            except ValueError as exc:
                diagnostics.append(str(exc))
        else:
            diagnostics.append('全文人物、地点与背景资料尚未保存')
        context = {
            'responsibility': 'review',
            'source_chunks': full_source_context(chunks),
            'world': world.model_dump(mode='json'),
            'required_event_ids': [event.id for event in world.events],
            'information': information or draft,
            'structural_errors': diagnostics,
            'review_scope': {'fact_chunks': sorted(open_chunks),
                             'retained_fact_chunks': sorted(checked)},
            'core_candidates': self.candidate_index(chunks),
            'current_time_groups': self.time_groups(world),
            'failed_jobs': self.failures,
            'last_delivery': state.feedback,
        }
        proposals = [{'original_id': proposal['original']['id'], 'proposed': proposal['proposed']}
                     for chunk in chunks
                     for proposal in self.storage.latest(f'chunk.{chunk.index}.proposals', [])]
        if proposals:
            context['second_pass_proposals'] = proposals
        if not information:
            context['information_schema'] = RepositoryInformation.model_json_schema()
        pending = self.pending_world()
        if any((pending.events, pending.entities, pending.locations, pending.objects)):
            context['pending_candidates'] = pending.model_dump(mode='json')
        return compact_evidence(context, chunks)

    def timeline_context(self, chunks, world, state):
        """时间比较保留全文、实际事件和引用身份，去掉无关档案、后果与历轮长历史。"""
        data = world.model_dump(mode='json', exclude={'changes', 'world_traits'})
        for event in data['events']:
            for field in ('goals', 'required_results', 'core_reason', 'time_node_id', 'event_order'):
                event.pop(field, None)
        data.pop('time_nodes')
        # 局部节点不是全局分组模板。只有跨块安排过的草稿才作为待核对候选提供。
        groups = self.time_groups(world) if state.time_placements else []
        saved_information = self.storage.latest('information') or {}
        local_time_drafts = []
        for chunk in chunks:
            topology = self.storage.latest(f'chunk.{chunk.index}.topology', {}) or {}
            local_time_drafts.append({
                'chunk': chunk.index,
                'nodes': [{key: node.get(key) for key in ('id', 'name', 'order', 'relative')}
                          for node in topology.get('time_nodes', []) if isinstance(node, dict)],
                'events': [{key: event.get(key) for key in
                            ('id', 'source_time', 'time_node_id', 'event_order', 'temporal_basis')}
                           for event in topology.get('events', []) if isinstance(event, dict)],
            })
        return compact_evidence({
            'responsibility': 'timeline', 'source_chunks': full_source_context(chunks), 'world': data,
            'story_overview': saved_information.get('story_overview'),
            'story_spine': saved_information.get('story_spine'),
            'local_time_drafts': local_time_drafts,
            'required_event_ids': [e.id for e in world.events], 'current_time_groups': groups,
            'current_diagnostics': timeline_errors(world) + temporal_errors(world),
            'last_delivery': state.feedback.get('timeline', {}),
        }, chunks)

    @staticmethod
    def time_groups(world):
        """展示已保存分组，供模型逐组比较；不推断、合并或改写任何位置。"""
        return [{**node.model_dump(mode='json'), 'events': [
                    {'id': e.id, 'order': e.event_order}
                    for e in sorted(world.events, key=lambda e: (e.event_order is None, e.event_order or 0))
                    if e.time_node_id == node.id]}
                for node in sorted(world.time_nodes, key=lambda n: (n.order is None, n.order or 0))]

    @staticmethod
    def apply_time_plan(plan, world, chunks):
        """专注时间和相关边；不以无关状态变化或档案错误拒绝合法时间修订。"""
        review = Review(is_valid=False, summary=plan.summary, issues=list(plan.issues), covered_chunks=[],
            correction=Correction(relations=plan.relations, relation_removals=plan.relation_removals), time=plan.time)
        revised = apply_correction(world, review, chunks)
        return apply_timeline(revised, plan.time or Timeline(), chunks, world.time_nodes)

    @staticmethod
    def validate_time_plan_delivery(plan):
        """只检查时间候选自身即可判断的重复与编号；语义留给完整上下文。"""
        if plan.time is None:
            return []
        nodes, placements = plan.time.nodes, plan.time.timeline
        errors = []
        node_ids = [node.id for node in nodes]
        if len(node_ids) != len(set(node_ids)):
            errors.append('time_nodes 节点编号重复')
        orders = [node.order for node in nodes if node.order is not None]
        if len(orders) != len(set(orders)):
            conflicts = {order: [node.id for node in nodes if node.order == order]
                         for order in set(orders) if orders.count(order) > 1}
            errors.append(f'nodes.order 不得重号；冲突为 {conflicts}；重新比较这些节点，不要改成未知以规避冲突')
        event_ids = [row.id for row in placements]
        if len(event_ids) != len(set(event_ids)):
            errors.append('time.timeline 事件编号重复')
        return errors

    @staticmethod
    def validate_review_delivery(review, _world, chunks, _required):
        """只退回候选自身的结构错误，语义疑点进入有原文的下一轮。"""
        expected = {chunk.index for chunk in chunks}
        actual = set(review.covered_chunks)
        if len(actual) != len(review.covered_chunks) or actual - expected:
            return [f'covered_chunks 必须是无重复的原文块编号；未知 {sorted(actual - expected)}']
        return []

    def candidate_index(self, chunks):
        """把阅读候选交给检查角色作为查漏线索，不要求逐条交付去向表。"""
        result = {}
        for chunk in chunks:
            reading = self.storage.latest(f'job:chunk.{chunk.index}.reading', {}).get('result', {})
            for index, candidate in enumerate(reading.get('core_candidates', []), 1):
                result[f'C{chunk.index}_K{index}'] = candidate
        return result

    @staticmethod
    def merged_entity_redirects(current, merges):
        """仅保存已合并的旧人物编号，避免后续把它当作新人使用。"""
        redirects = dict(current)
        redirects.update({merge.source: merge.target for merge in merges})
        return redirects

    def global_time_ids(self, world, review):
        """局部节点不算全局完成；只记录跨块阶段明确安排的事件。"""
        ids = set(self.storage.get('review_state', {}).get('time_placements', []))
        if review.time is not None:
            ids.update(row.id for row in review.time.timeline)
        return ids & {event.id for event in world.events if event.time_node_id is not None}

    def advance_fact_review(self, state, review, previous, candidate, chunks, allow_new=True):
        """逐块查漏的完成记录只随相应事实变化而失效。"""
        before, after = fact_dependencies(previous, chunks), fact_dependencies(candidate, chunks)
        state.checked_facts = {index: version for index, version in state.checked_facts.items()
                               if index in after and digest(after[index]) == version}
        if allow_new:
            state.checked_facts.update({index: digest(after[index]) for index in review.covered_chunks
                                        if index in after and (review.is_valid or before[index] == after[index])})
        state.entity_redirects = self.merged_entity_redirects(
            state.entity_redirects, review.correction.entity_merges)

    @staticmethod
    def _remove_incomplete_merge_catalog(correction, information, gaps):
        """移除失败合并的目录/档案包，分开source与target供后续精确隔离。"""
        sources = {'entities': set(), 'locations': set(), 'objects': set()}
        invalid = {}
        for merge_field, catalog_field, profile_field, merge, _ in gaps:
            sources[catalog_field].add(merge.source)
            invalid.setdefault((merge_field, catalog_field, profile_field), set()).add(
                (merge.source, merge.target))
        for (merge_field, catalog_field, profile_field), pairs in invalid.items():
            bundle_ids = {key for pair in pairs for key in pair}
            setattr(correction, merge_field,
                    [merge for merge in getattr(correction, merge_field)
                     if (merge.source, merge.target) not in pairs])
            for key in bundle_ids:
                getattr(correction, catalog_field).pop(key, None)
                if profile_field and isinstance(information, dict):
                    profiles = information.get(profile_field)
                    if isinstance(profiles, dict):
                        profiles.pop(key, None)
        return sources

    @staticmethod
    def _event_catalog_references(event):
        return {
            'entities': set(event.actor_ids) | set(event.target_ids),
            'locations': {event.at_location_id} if event.at_location_id else set(),
            'objects': set(event.object_ids),
        }

    @staticmethod
    def _submitted_ids(value):
        if isinstance(value, list):
            return {item for item in value if isinstance(item, str)}
        return {value} if isinstance(value, str) else set()

    @classmethod
    def _remove_merge_coupled_facts(cls, correction, world, pending, sources):
        """仅隔离引用source或明确把source换成target的事实修订。"""
        def touches_source(event):
            refs = cls._event_catalog_references(event)
            return any(refs[field] & sources[field] for field in sources)

        # apply_correction同样以已保存world为优先，pending不能覆盖同编号正式事件。
        originals = {event.id: event for event in [*pending.events, *world.events]}
        affected_events = {event.id for event in originals.values() if touches_source(event)}
        kept_events = []
        for event in correction.events:
            if event.id in affected_events or touches_source(event):
                affected_events.add(event.id)
            else:
                kept_events.append(event)
        correction.events = kept_events
        for key, fields in list(correction.event_updates.items()):
            explicit = {
                'entities': cls._submitted_ids(fields.get('actor_ids')) | cls._submitted_ids(fields.get('target_ids')),
                'locations': cls._submitted_ids(fields.get('at_location_id')),
                'objects': cls._submitted_ids(fields.get('object_ids')),
            }
            if key in affected_events or any(explicit[field] & sources[field] for field in sources):
                affected_events.add(key)
                correction.event_updates.pop(key)
        correction.merges = [row for row in correction.merges
                             if row.source not in affected_events and row.target not in affected_events]
        correction.removals = [row for row in correction.removals if row.id not in affected_events]
        if correction.relations is not None:
            correction.relations = [row for row in correction.relations
                                    if not {row.source, row.target} & affected_events]
        correction.relation_removals = [row for row in correction.relation_removals
                                        if not {row.source, row.target} & affected_events]
        correction.changes = ([row for row in correction.changes
                               if row.event_id not in affected_events
                               and not any(row.subject_id in sources[field] for field in sources)]
                              if correction.changes is not None else None)
        correction.change_removals = [row for row in correction.change_removals
                                      if row.event_id not in affected_events
                                      and not any(row.subject_id in sources[field] for field in sources)]
        return originals, affected_events

    @staticmethod
    def _remove_merge_coupled_time(review, world, originals, affected_events):
        if review.time is not None:
            # 完整嵌套timeline包含所有事件。回收时转为平面增量：去掉
            # 受影响事件及其节点改写，但保留其他事件的独立位置修订。
            affected_nodes = {event.time_node_id for key, event in originals.items()
                              if key in affected_events and event.time_node_id}
            affected_nodes.update(row.node_id for row in review.time.timeline
                                  if row.id in affected_events)
            safe_rows = [row for row in review.time.timeline if row.id not in affected_events]
            shared_new_nodes = {row.node_id for row in safe_rows}
            safe_nodes = [node for node in review.time.nodes
                          if node.id not in affected_nodes or node.id in shared_new_nodes]
            known_nodes = {node.id for node in world.time_nodes} | {node.id for node in safe_nodes}
            safe_placements = [row for row in safe_rows if row.node_id in known_nodes]
            review.time = (Timeline(nodes=safe_nodes, timeline=safe_placements)
                           if safe_nodes or safe_placements else None)

    @classmethod
    def _isolate_merge_bundles(cls, review, world, gaps, pending=None, current_information=None):
        """只移除失败的身份合并及其依赖，保留同轮无关事实和时间修订。"""
        correction = review.correction.model_copy(deep=True)
        information = deepcopy(review.information)
        sources = cls._remove_incomplete_merge_catalog(correction, information, gaps)
        pending = pending or World(entities={}, locations={}, objects={}, world_traits={})
        originals, affected_events = cls._remove_merge_coupled_facts(
            correction, world, pending, sources)

        # information.relations是整表替换。原表或新表仍引用source时，
        # 该字段与失败合并耦合；单纯引用既有target则保留。
        source_entities = sources['entities']
        relation_sets = []
        if isinstance(current_information, dict):
            relation_sets.append(current_information.get('relations'))
        if isinstance(information, dict):
            relation_sets.append(information.get('relations'))
        if source_entities and any(isinstance(rows, list) and any(
                isinstance(row, dict)
                and {value for value in (row.get('owner'), row.get('other')) if isinstance(value, str)}
                & source_entities for row in rows) for rows in relation_sets):
            if isinstance(information, dict):
                information.pop('relations', None)

        cls._remove_merge_coupled_time(review, world, originals, affected_events)
        review.correction = correction
        review.information = information

    @classmethod
    def reject_incomplete_merge_bundles(cls, review, world, pending=None, current_information=None):
        """原子隔离缺项合并；缺项本身进入有原文的下一轮语义修正。"""
        gaps = merge_bundle_gaps(review.correction, review.information)
        if not gaps:
            return []
        errors = merge_bundle_errors(review.correction, review.information)
        cls._isolate_merge_bundles(review, world, gaps, pending, current_information)
        return errors

    @staticmethod
    def merge_bundles(correction, require_profile=False):
        """把合并转为统一隔离描述；档案失败只涉及人物和地点合并。"""
        return [
            (merge_field, catalog_field, profile_field, merge, [])
            for merge_field, catalog_field, profile_field in (
                ('entity_merges', 'entities', 'characters'),
                ('location_merges', 'locations', 'locations'),
                ('object_merges', 'objects', None),
            )
            if not require_profile or profile_field is not None
            for merge in getattr(correction, merge_field)
        ]

    def apply_review_correction(self, world, review, chunks, pending):
        """应用事实补丁并执行接收所需的确定性结构检查。"""
        revised = apply_correction(world, review, chunks, pending)
        retired = set(ReviewState.model_validate(
            self.storage.get('review_state', {})).entity_redirects) & revised.entities.keys()
        if retired:
            raise ValueError(f'已合并的人物编号不可重新使用：{sorted(retired)}；'
                             '引用现存target，或为不同人物分配全新编号')
        normalize_evidence(revised.events, chunks)
        normalize_references(revised, revised)
        graph_only = revised.model_copy(update={'time_nodes': []})
        invalid = register_errors(revised) + topology_errors(
            graph_only, graph_only, chunks, check_time=True)
        if invalid:
            raise ValueError('\n'.join(invalid))
        unclassified = sorted(event.id for event in revised.events
                              if event.occurrence_layer == '未分类')
        if unclassified:
            raise ValueError(f'事件缺少明确occurrence_layer：{unclassified}')
        return revised

    def preserve_event_bundles(self, review, world, chunks, pending, patch):
        """整批失败时，只保全逐个通过原有校验的事件修订及其合并。"""
        candidate, accepted, rejected = world.model_copy(deep=True), Correction(), []
        for key, fields in patch.event_updates.items():
            facts = {name: value for name, value in fields.items()
                     if name not in ('time_node_id', 'event_order')}
            if not facts:
                continue
            merges = [merge for merge in patch.merges if merge.target == key]
            isolated = review.model_copy(update={
                'correction': Correction(event_updates={key: facts}, merges=merges), 'time': None}, deep=True)
            try:
                revised = self.apply_review_correction(candidate, isolated, chunks, pending)
            except ValueError as exc:
                rejected.append(f'{key}: {str(exc).splitlines()[0]}')
            else:
                candidate = revised
                accepted.event_updates[key] = facts
                accepted.merges.extend(merges)
        return candidate, accepted, rejected

    def _apply_review_facts(self, review, world, chunks, pending, current_information, errors):
        """接收事实修订；失败时只隔离能独立验证的安全子集。"""
        saved_event_ids = {event.id for event in world.events}
        new_events = [event for event in review.correction.events if event.id not in saved_event_ids]
        try:
            return self.apply_review_correction(world, review, chunks, pending)
        except ValueError as exc:
            patch = review.correction
            catalog_change = any((patch.entities, patch.locations, patch.objects, patch.world_traits,
                                  patch.entity_merges, patch.location_merges, patch.object_merges))
            errors.append(('目录修订未接收：' if catalog_change else '事实修订未接收：') + str(exc))
            submitted_merges = self.merge_bundles(patch)
            if submitted_merges:
                self._isolate_merge_bundles(
                    review, world, submitted_merges, pending, current_information)
                try:
                    candidate = self.apply_review_correction(world, review, chunks, pending)
                except ValueError as isolated:
                    errors.append('合并隔离后的独立事实修订未接收：' + str(isolated))
                    review.correction = Correction()
                    candidate = world.model_copy(deep=True)
            else:
                review.correction = Correction()
                candidate = world.model_copy(deep=True)
            if new_events and not submitted_merges and review.correction == Correction():
                isolated = review.model_copy(update={'correction': Correction(events=new_events),
                                                     'time': None}, deep=True)
                try:
                    candidate = self.apply_review_correction(world, isolated, chunks, pending)
                except ValueError as isolated_error:
                    errors.append('独立新增事件未接收：' + str(isolated_error))
                else:
                    review.correction, review.time = isolated.correction, None
                    errors.append('已保存独立新增事件；其余事实修订仍待处理')
            elif (not submitted_merges and review.correction == Correction()
                  and patch.event_updates
                  and self.storage.get('review_state', {}).get('rounds', 0) + 1
                  < getattr(self, 'review_rounds', 6)):
                candidate, accepted, rejected = self.preserve_event_bundles(
                    review, world, chunks, pending, patch)
                errors.extend('独立事实修订未接收：' + item for item in rejected)
                if accepted.event_updates:
                    review.correction, review.time = accepted, None
                    errors.append('已保存独立事件修订；其余事实、关系及时间修订仍待处理')
            return candidate

    @staticmethod
    def _apply_review_time(candidate, review, chunks, previous_nodes):
        """独立接收时间补丁，保留提交副本供身份合并隔离后重放。"""
        submitted = review.time.model_copy(deep=True) if review.time is not None else None
        if review.time is None:
            return candidate, submitted, None
        try:
            candidate = apply_timeline(candidate, review.time, chunks, previous_nodes)
        except ValueError as exc:
            review.time = None
            return candidate, submitted, str(exc)
        return candidate, submitted, None

    def _apply_review_information(self, candidate, review, world, chunks, pending,
                                  current_information, submitted_time, time_error, errors):
        """接收档案；档案失败时回收与身份合并耦合的事实和时间。"""
        try:
            details = apply_information(
                current_information, review, candidate, complete=False).model_dump(mode='json')
            return candidate, details, time_error
        except ValueError as exc:
            errors.append('档案修订未接收：' + str(exc))
        profile_merges = self.merge_bundles(review.correction, require_profile=True)
        if not profile_merges:
            review.information = None
            return candidate, current_information, time_error
        review.time = submitted_time
        self._isolate_merge_bundles(
            review, world, profile_merges, pending, current_information)
        review.information = None
        try:
            candidate = self.apply_review_correction(world, review, chunks, pending)
        except ValueError as isolated:
            errors.append('合并隔离后的独立事实修订未接收：' + str(isolated))
            candidate, review.correction = world.model_copy(deep=True), Correction()
        if review.time is not None:
            try:
                candidate = apply_timeline(candidate, review.time, chunks, world.time_nodes)
            except ValueError as isolated:
                errors.append('合并隔离后的独立时间修订未接收：' + str(isolated))
                review.time = None
        return candidate, current_information, None

    @staticmethod
    def _review_result_errors(review, candidate, details, chunks):
        """汇总接收后的结构问题；不在这里改变事实或时间。"""
        errors = topology_errors(candidate, candidate, chunks, check_time=True) + timeline_errors(candidate)
        for event in candidate.events:
            for index, source in enumerate(event.evidence):
                if source.unverified_quote:
                    errors.append(f'{event.id}.evidence[{index}] 的复抄引文与来源不符；'
                                  '核对回填原文后用event_updates.evidence提交正确片段编号，不复抄quote')
        if details:
            try:
                parsed_information = RepositoryInformation.model_validate(details)
            except ValueError as exc:
                errors.append('档案结构仍待修复：' + str(exc))
            else:
                errors.extend(information_errors(parsed_information, candidate))
        expected, actual = set(range(1, len(chunks) + 1)), set(review.covered_chunks)
        if actual - expected:
            errors.append(f'covered_chunks 多余 {sorted(actual - expected)}')
        if review.is_valid:
            if expected - actual:
                errors.append(f'原文查漏尚未覆盖文本块 {sorted(expected - actual)}')
            errors.extend(timeline_errors(candidate, require_complete=True))
            if not review.time_valid:
                errors.append('time_valid=false：请指出并修正具体故事时间错误，或根据原文完成核对')
        return errors

    def evaluate_review(self, review, world, chunks):
        """事实图、时间及档案分别校验，安全进展可保存；任何未解决问题仍阻止完整验收。"""
        # issues 是模型主动发现但尚未解决的语义问题。即使模型误把
        # is_valid 写成 true，也保留问题并按未完成处理，不能让无原文
        # 的格式修复通过删除 issues 来制造成功。
        if review.issues:
            review.is_valid = False
        # 历史失败只有完成的整体复核才能销项；模型不再逐项声明。
        review.resolved_jobs = []
        candidate, errors = world.model_copy(deep=True), []
        pending = self.pending_world()
        current_information = (self.storage.latest('information')
                               or self.storage.latest('foundation.result', {}).get('information') or {})
        errors.extend('档案修订未接收：' + error
                      for error in self.reject_incomplete_merge_bundles(
                          review, world, pending, current_information))
        candidate = self._apply_review_facts(
            review, world, chunks, pending, current_information, errors)
        candidate, submitted_time, time_error = self._apply_review_time(
            candidate, review, chunks, world.time_nodes)
        candidate, details, time_error = self._apply_review_information(
            candidate, review, world, chunks, pending, current_information,
            submitted_time, time_error, errors)
        if time_error is not None:
            errors.append('时间修订未接收：' + time_error)
        errors.extend(self._review_result_errors(review, candidate, details, chunks))
        if errors:
            review.is_valid = False
            review.issues = list(dict.fromkeys([*review.issues, *errors]))
        return candidate, details

    def update_time_confirmations(self, state, review, previous, candidate, chunks=None, allow_new=True):
        """完整时间线经一次语义审读后确认；修改后的版本必须再核对。"""
        after = time_dependencies(candidate)
        signatures = {key: digest(value) for key, value in after.items()}
        state.confirmed_times = {key: value for key, value in state.confirmed_times.items()
                                 if signatures.get(key) == value}
        covered = chunks is not None and set(review.covered_chunks) == {chunk.index for chunk in chunks}
        delivery_failed = any(issue.startswith('交付未通过程序校验：') for issue in review.issues)
        if (allow_new and review.time_valid and covered and not delivery_failed
                and time_dependencies(previous) == after
                and not timeline_errors(candidate, require_complete=True)):
            state.confirmed_times = signatures

    @staticmethod
    def _review_round_available(state, review_rounds):
        normal = state.rounds < review_rounds
        final_handoff = (state.rounds < review_rounds + 2
                         and len(state.history) >= review_rounds
                         and state.history[review_rounds - 1].get('final_time_handoff'))
        return normal or final_handoff

    async def _request_global_delivery(self, task, job, chunks, world, state):
        """执行一轮时间或检查请求；失败候选仍按原协议保留供评估。"""
        if task == 'review':
            await self.supplement_information(chunks, world)
        information = self.storage.latest('information')
        context = (self.timeline_context(chunks, world, state)
                   if task == 'timeline' else self.review_context(chunks, world))
        schema = TimelinePlan if task == 'timeline' else Review
        validate = (self.validate_time_plan_delivery if task == 'timeline' else
                    lambda result: self.validate_review_delivery(
                        result, world, chunks, {event.id for event in world.events}))
        try:
            delivery = await self.agent.run(
                job, 'check',
                prompt('NovelTimelinePrompt.md' if task == 'timeline' else 'NovelCheckPrompt.md'),
                context, schema, validate)
        except RunStopped:
            raise
        except JobFailed as exc:
            self.log.write('error', job=job, error=str(exc))
            try:
                saved = self.storage.latest('job:' + job, {})
                submitted = saved.get('submitted_candidate', saved.get('candidate', {}))
                delivery = schema.model_validate(submitted)
                if task == 'review':
                    delivery.is_valid = False
                    delivery.issues = list(dict.fromkeys([
                        *delivery.issues,
                        '交付未通过程序校验：' + str(saved.get('error') or exc)]))
            except ValueError:
                delivery = None
        return delivery, information

    def _evaluate_global_delivery(self, task, delivery, chunks, world, information):
        """把模型交付转换为本轮候选；语义确认仍只由后续 review 完成。"""
        if task == 'review':
            review = delivery or Review(
                is_valid=False, summary='检查任务未完整交付', issues=[], covered_chunks=[])
            model_issues = list(review.issues)
            candidate, details = self.evaluate_review(review, world, chunks)
            return review, candidate, details, model_issues
        review = Review(is_valid=False,
                        summary=delivery.summary if delivery else '时间任务未完整交付',
                        issues=list(delivery.issues) if delivery else [], covered_chunks=[])
        candidate, details = world.model_copy(deep=True), information
        model_issues = list(review.issues)
        if delivery is None:
            return review, candidate, details, model_issues
        try:
            candidate = self.apply_time_plan(delivery, world, chunks)
            review.time = delivery.time
        except ValueError as exc:
            review.issues.append('时间修订未接收：' + str(exc))
            if '完整分组缺少事件' in str(exc) and delivery.time is not None:
                partial = delivery.time.model_copy(update={'complete': False})
                try:
                    candidate = self.apply_time_plan(
                        delivery.model_copy(update={'time': partial}), world, chunks)
                    review.time = partial
                except ValueError:
                    pass
        return review, candidate, details, model_issues

    def _advance_review_state(self, state, task, review, world, candidate, details,
                              information, chunks, model_issues, review_rounds):
        """推进一轮纯状态转换，返回仍未解决的任务及是否出现空转。"""
        changed = substantive_world(candidate) != substantive_world(world) or details != information
        self.update_time_confirmations(state, review, world, candidate, chunks)
        if task == 'review':
            self.advance_fact_review(state, review, world, candidate, chunks)
        state.time_placements = sorted(self.global_time_ids(candidate, review))
        complete_time_errors = timeline_errors(candidate, require_complete=True)
        state.time_reviewed = (set(state.time_placements) == {event.id for event in candidate.events}
                               and not complete_time_errors)
        facts_complete = set(state.checked_facts) == {chunk.index for chunk in chunks}
        times_complete = set(state.confirmed_times) == {event.id for event in candidate.events}
        ready = (task == 'review' and review.is_valid and state.time_reviewed
                 and details is not None and facts_complete and times_complete)
        if ready:
            review.resolved_jobs = sorted({failure['job'] for failure in self.failures})
        remaining = [failure for failure in self.failures
                     if failure['job'] not in review.resolved_jobs]
        state.accepted = ready and not remaining
        program_feedback = [issue for issue in review.issues if issue not in model_issues]
        signature = digest({
            'task': task,
            'world': substantive_world(candidate),
            'information': details,
            'resolved_jobs': sorted(set(state.resolved_jobs) | set(review.resolved_jobs)),
            'checked_facts': state.checked_facts,
            'time_placements': sorted(self.global_time_ids(candidate, review)),
            'confirmed_times': sorted(state.confirmed_times),
            'program_feedback': program_feedback,
        })
        repeated = signature in state.signatures and not state.accepted
        state.signatures.append(signature)
        needs_time = (not state.time_reviewed
                      or any(issue.startswith('时间修订未接收：') for issue in program_feedback))
        state.next_task = 'timeline' if task == 'review' and needs_time else 'review'
        state.rounds += 1
        state.resolved_jobs = sorted(set(state.resolved_jobs) | set(review.resolved_jobs))
        state.feedback[task] = {'summary': review.summary, 'model_issues': model_issues,
                                'program_feedback': program_feedback}
        old_ids = {event.id for event in world.events}
        final_time_handoff = (state.rounds == review_rounds and task == 'review'
                              and any(event.id not in old_ids and
                                      (event.time_node_id is None or event.event_order is None)
                                      for event in candidate.events))
        state.history.append({
            'round': state.rounds, 'task': task, 'changed': changed,
            'issues': review.issues, 'summary': review.summary,
            'resolved_jobs': review.resolved_jobs,
            'final_time_handoff': final_time_handoff,
        })
        return remaining, repeated

    def _save_review_round(self, job, state, review, candidate, details, remaining):
        """世界、复核、档案、游标和失败清单保持一次事务提交。"""
        with self.storage.transaction():
            self.storage.snapshot('world', candidate.model_dump(mode='json'))
            self.storage.snapshot('validation.review', review.model_dump(mode='json'))
            self.storage.snapshot('information', details)
            self.storage.set('review_state', state.model_dump(mode='json'))
            self.storage.snapshot('failures', remaining)
        self.log.write('stage', job=job, status='saved', globally_placed=len(state.time_placements),
                       time_confirmed=len(state.confirmed_times), events=len(candidate.events),
                       next_task=state.next_task)

    async def reconcile(self, chunks, world):
        """时间草稿后做一次广义审计；只为具体未决错误继续修复。"""
        state = ReviewState.model_validate(self.storage.get('review_state', {}))
        if state.accepted:
            return world, True
        review_rounds = getattr(self, 'review_rounds', 6)
        while self._review_round_available(state, review_rounds):
            task = state.next_task if world.events else 'review'
            job = f'global.{task}.{sum(h["task"] == task for h in state.history)}'
            self.stage('跨块合并与时间校验', round=state.rounds + 1, responsibility=task)
            delivery, information = await self._request_global_delivery(
                task, job, chunks, world, state)
            review, candidate, details, model_issues = self._evaluate_global_delivery(
                task, delivery, chunks, world, information)
            remaining, repeated = self._advance_review_state(
                state, task, review, world, candidate, details, information,
                chunks, model_issues, review_rounds)
            self._save_review_round(job, state, review, candidate, details, remaining)
            world, self.failures = candidate, remaining
            if state.accepted:
                return world, True
            if repeated:
                self.failed('global.review', '重复出现同一未完成资料，停止空转；已有结果保留')
                return world, False
        self.failed('global.review', f'跨块合并与时间校验达到{state.rounds}轮，尚未获得稳定完整交付')
        return world, False

    async def extract(self, text):
        # 全局整理中断后直接恢复该阶段，不重组旧块覆盖已接收修订。
        if self.storage.get('assembly_complete'):
            self.failures = self.storage.latest('failures', [])
            world = saved_world(self.storage)
            return await self.reconcile(chunk_text(text), world)
        register = await self.foundation(text)
        if register is None:
            return saved_world(self.storage), False
        self.stage('文本分块')
        chunks = chunk_text(text)
        self.storage.snapshot('chunks', [chunk.payload() for chunk in chunks])
        self.stage('分块核心事件、关系及局部时间提取', chunks=len(chunks))
        saved_information = self.storage.latest('information')
        information = RepositoryInformation.model_validate(saved_information) if saved_information else None
        semaphore = asyncio.Semaphore(self.concurrency)
        results = await asyncio.gather(*(self.extract_chunk(chunk, chunks, register, information, semaphore)
                                         for chunk in chunks))
        world = World(**register.model_dump())
        for result in results:
            world = self.combine(world, result)
        # 保留模型的局部分组与节点内顺序。各块的节点序号不属于同一坐标系，
        # 暂不当作全局先后；原序号仍在块快照/校验上下文中，由模型跨块对齐。
        for node in world.time_nodes:
            node.order = None
        self.stage('结构校验与修复')
        self.storage.snapshot('structural_validation', register_errors(world) + topology_errors(world, world, chunks))
        with self.storage.transaction():
            self.storage.snapshot('world', world.model_dump(mode='json'))
            self.storage.set('assembly_complete', True)
        return await self.reconcile(chunks, world)
