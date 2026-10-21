"""本项目的数据协议；程序验证引用与时间结构，不代替模型判断事实。"""
from collections import Counter
from collections.abc import Iterable
from copy import deepcopy
import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_serializer, model_validator
from pydantic.json_schema import SkipJsonSchema

class Record(BaseModel):
    # 无歧义的多余字段不阻断入库；原始响应始终另行保存。
    model_config = {'extra': 'ignore', 'str_strip_whitespace': True}

    @field_validator('*', mode='before')
    @classmethod
    def empty_collections(cls, value, info):
        factory = cls.model_fields[info.field_name].default_factory
        if factory in (dict, list) and (value is None or isinstance(value, (dict, list)) and not value):
            return factory()
        return value

    @field_validator('order', 'event_order', mode='before', check_fields=False)
    @classmethod
    def numeric_start(cls, value):
        if isinstance(value, bool):
            raise ValueError('开始刻度不能是布尔值')
        return value


class Entity(Record):
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    description: str = ''


class Location(Entity):
    pass


class Object(Entity):
    # 历史目录可读；新提取的归属与位置进入初始状态及事件delta。
    location_id: SkipJsonSchema[str | None] = None
    owner_name: SkipJsonSchema[str | None] = None
    function: str | None = None
    significance: str | None = None
    evidence: list['Evidence'] = Field(default_factory=list)


class Locations(Record):
    locations: dict[str, Location]


class Objects(Record):
    objects: dict[str, Object]


class Traits(Record):
    world_traits: dict[str, str]


class Entities(Record):
    entities: dict[str, Entity]


class Register(Locations, Objects, Traits, Entities):
    pass


class Evidence(Record):
    model_config = {'json_schema_extra': {'required': ['chunk', 'passage_ids']}}
    chunk: int = Field(ge=1)
    # 模型只选来源编号，原文由程序回填；旧引文仍可读取，冲突保留待核实。
    quote: SkipJsonSchema[str] = ''
    unverified_quote: SkipJsonSchema[str | None] = None
    passage_ids: list[str] = Field(default_factory=list,
        description='选择支持此事的原文片段编号；程序回填完整原文，不复抄或概括引文',
        json_schema_extra={'minItems': 1})


class ReferenceState(Record):
    status: Literal['known', 'none', 'unknown']
    id: str | None = None

    @model_validator(mode='after')
    def consistent_reference(self):
        if (self.status == 'known' and not self.id) or (self.status != 'known' and self.id is not None):
            raise ValueError('known须提供编号；none/unknown的id须为null')
        return self


RelationStatus = Literal['active', 'inactive', 'unknown']


class RelationIdentity(Record):
    owner_id: str
    other_id: str
    relation: str = Field(min_length=1, description='other相对于owner的关系；每项只写一种关系')


class RelationState(RelationIdentity):
    status: RelationStatus
    detail: str | None = None
    evidence: list[Evidence] = Field(min_length=1)


class ObjectState(Record):
    owner: ReferenceState = Field(default_factory=lambda: ReferenceState(status='unknown'))
    holder: ReferenceState = Field(default_factory=lambda: ReferenceState(status='unknown'))
    location: ReferenceState = Field(default_factory=lambda: ReferenceState(status='unknown'))
    condition: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class AssetState(Record):
    relations: list[RelationState] = Field(default_factory=list)
    objects: dict[str, ObjectState] = Field(default_factory=dict)


class StatePatch(Record):
    model_config = {'extra': 'forbid'}

    @model_serializer(mode='wrap')
    def supplied_fields_only(self, handler):
        # 保存/恢复必须区分“未修改”与“明确改为未知”，不能补出省略字段。
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}

    @model_validator(mode='after')
    def nonempty_patch(self):
        if not self.model_fields_set:
            raise ValueError('set须至少包含一个实际更新字段')
        if any(getattr(self, key) is None for key in self.model_fields_set - {'detail', 'condition'}):
            raise ValueError('状态或引用不能整体为null；未知用unknown，明确无人用none')
        return self


class RelationStatePatch(StatePatch):
    status: RelationStatus | SkipJsonSchema[None] = None
    detail: str | None = None


class ObjectStatePatch(StatePatch):
    owner: ReferenceState | SkipJsonSchema[None] = None
    holder: ReferenceState | SkipJsonSchema[None] = None
    location: ReferenceState | SkipJsonSchema[None] = None
    condition: str | None = None


class RelationDelta(RelationIdentity):
    set: RelationStatePatch
    basis: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)


class ObjectDelta(Record):
    object_id: str
    set: ObjectStatePatch
    basis: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)


class StateDelta(Record):
    relations: list[RelationDelta] = Field(default_factory=list)
    objects: list[ObjectDelta] = Field(default_factory=list)


OccurrenceLayer = (Literal['实际发生', '讲述', '获知', '意图/计划']
                   | SkipJsonSchema[Literal['未分类']])


class OccurrenceSignature(Record):
    """一次发生的稳定身份；候选、最终事件和核对记录共同使用。"""
    model_config = {'json_schema_extra': {
        'required': ['actual_subjects', 'occurrence', 'start_anchor', 'layer'],
    }}
    actual_subjects: list[str] = Field(default_factory=list,
        description='实际实施或经历此事的人物姓名或目录编号，不写转述者代替实际主体',
        json_schema_extra={'minItems': 1})
    occurrence: str = Field(min_length=1, description='一次实际发生、结果、言语或状态变化')
    start_anchor: str = Field(min_length=1, description='故事世界中的开始阶段或原文相对时间线索')
    layer: OccurrenceLayer


class Event(Record):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1, description='实际发生；不同经历不压成摘要，转述不替代被转述的前事')
    actor_ids: list[str] = Field(default_factory=list, description='直接实施或经历此事的人物，包括死亡者、受害者；不能只填转述者')
    target_ids: list[str] = Field(default_factory=list, description='事件明确指向或影响的人物，不因同场出现就添加')
    at_location_id: str | None = None
    object_ids: list[str] = Field(default_factory=list)
    source_time: str | None = Field(default=None, description='实际发生的原文时间线索，不以讲述或获知时刻代替')
    time_node_id: str | None = None
    event_order: int | None = Field(default=None, ge=1)
    temporal_basis: str = Field(min_length=1, description='实际开始的原文锚点及与其他发生的先后；区分回忆中的过去和讲述现场')
    core_reason: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    epistemic_status: str = Field(description='事实、自述、传闻、猜想、计划等；描述中也须保留影响理解的限定')
    goals: dict[str, str] = Field(default_factory=dict,
        description='参与者人物编号到其目的，例如 {"ENT_1":"离开此地"}；键不能是目的名称，未知目的用空对象')
    required_results: list[str] | None = None
    state_delta: StateDelta | None = Field(default=None,
        description='consequences交付后保存；null表示尚未检查状态变化，空relations/objects表示确无变化')
    occurrence_layer: OccurrenceLayer = Field(
        description='故事世界中的发生层次；与epistemic_status的证据性质分开，选择实际发生、讲述、获知或意图/计划')
    occurrence_signature: OccurrenceSignature | SkipJsonSchema[None] = Field(
        description='本事件的稳定发生身份；新事件必填，并与reading候选、合并及删除核对共用')

    @model_validator(mode='before')
    @classmethod
    def legacy_occurrence_layer(cls, value):
        """旧工作区仍可只读；新交付由Workflow拒绝“未分类”。"""
        if isinstance(value, dict):
            value = dict(value)
            value.setdefault('occurrence_layer', '未分类')
            value.setdefault('occurrence_signature', None)
        return value


class Relation(Record):
    source: str
    target: str
    kind: Literal['causes', 'enables', 'prevents', 'before', 'simultaneous']
    basis: str = Field(min_length=1)


class Change(Record):
    event_id: str
    subject_id: str
    change: str = Field(min_length=1)
    basis: str = Field(min_length=1)


class TimeNode(Record):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    order: int | None = Field(ge=1)
    relative: str | None = Field(default=None, description='实际阶段及排序依据的说明；新开始锚点须保存到事件source_time/evidence或关系，不能只改说明')


class Topology(Record):
    events: list[Event] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    changes: list[Change] = Field(default_factory=list)
    time_nodes: list[TimeNode] = Field(default_factory=list)


class CoreCandidate(Record):
    description: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    signature: OccurrenceSignature
    core_reason: str = Field(min_length=1,
        description='删除或改变此事会使哪个具体转折、人物命运或结局无法发生或发生不同，以及因果依据')

    @model_validator(mode='before')
    @classmethod
    def legacy_signature(cls, value):
        """历史失败候选可恢复；当前reading校验不会接受未分类签名。"""
        if isinstance(value, dict) and 'signature' not in value:
            text = str(value.get('description') or '历史候选')
            value = {**value,
                'signature': {'actual_subjects': [], 'occurrence': text,
                              'start_anchor': '历史候选未结构化', 'layer': '未分类'},
                'core_reason': value.get('core_reason') or '历史候选未结构化'}
        return value


class Reading(Record):
    who: str
    what: str
    where: str
    when: str
    why: str
    how: str
    core_candidates: list[CoreCandidate]
    uncertainties: list[str] = Field(default_factory=list)


class EventPass(Topology):
    entities: dict[str, Entity] = Field(default_factory=dict, description='原文中目录漏识别的人物；仅新增，不改写已有编号')
    locations: dict[str, Location] = Field(default_factory=dict)
    objects: dict[str, Object] = Field(default_factory=dict)
    coverage: str = Field(min_length=1, description='本职责的核心候选与遗漏检查；确实无核心发生也说明理由')


class Consequences(Record):
    changes: list[Change]
    state_deltas: dict[str, StateDelta] = Field(default_factory=dict,
        description='每个现存event_id到关系/物品变化；没有变化也交空relations/objects，不写整份快照')
    coverage: str = Field(min_length=1)


class World(Register, Topology):
    pass


class EventMerge(Record):
    source: str
    target: str
    reason: str = Field(min_length=1)


class EventRemoval(Record):
    id: str = Field(description='本轮world中要删除的事件编号；程序从该快照绑定旧发生，不复抄签名')
    reason: str = Field(min_length=1)
    # 兼容历史交付；显式附带旧签名时仍须精确匹配，不能静默忽略冲突。
    signature: SkipJsonSchema[OccurrenceSignature | None] = None


class TimePlacement(Record):
    id: str
    node_id: str
    order: int | None = Field(ge=1)
    basis: str = Field(min_length=1, description='原文开始时间线索及前后比较；不能用文本位置或复述事件代替')


class Timeline(Record):
    nodes: list[TimeNode] = Field(default_factory=list, description='新增或调整的节点；未提交的保留')
    timeline: list[TimePlacement] = Field(default_factory=list, description='新增或调整的事件位置；未提交的保留，最终必须完整')
    complete: SkipJsonSchema[bool] = False


class GroupedEvent(Record):
    id: str
    order: int = Field(ge=1, description='只在本节点内比较开始先后；同号表示同时')
    basis: str = Field(min_length=1, description='原文开始线索及与本组其他事件的比较，不需要精确日期')


class TimeGroup(Record):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    relative: str = Field(min_length=1, description='实际阶段和相邻比较的说明；新开始锚点写入事件source_time/evidence或关系，不以改写说明代替修正位置')
    events: list[GroupedEvent] = Field(min_length=1)


class GroupedTimeline(Record):
    """模型直接交付排好先后的分组；程序仅把列表位置编码为节点序号。"""
    nodes: list[TimeGroup] = Field(min_length=1,
        description='完整事件分组，按实际发生从早到晚排列；每件事件恰好一次。列表位置就是节点order=1,2,3…')

    def flatten(self):
        return Timeline(complete=True,
            nodes=[TimeNode(id=n.id, name=n.name, relative=n.relative, order=i)
                   for i, n in enumerate(self.nodes, 1)],
            timeline=[TimePlacement(id=e.id, node_id=n.id, order=e.order, basis=e.basis)
                      for n in self.nodes for e in n.events])


def time_delivery(value):
    # 接收嵌套新协议；内部/历史的平面增量结构仍可读。嵌套错误不能退化成空平面提交。
    if isinstance(value, GroupedTimeline):
        return value.flatten()
    if isinstance(value, dict) and ('timeline' not in value or
            any(isinstance(n, dict) and 'events' in n for n in value.get('nodes') or [])):
        grouped = GroupedTimeline.model_validate(value)
        canonical = grouped.flatten()
        if 'timeline' in value and Timeline.model_validate(value).timeline != canonical.timeline:
            raise ValueError('嵌套事件分组与冗余timeline冲突；只交付一个明确的分组结果')
        for index, node in enumerate(value['nodes'], 1):
            if 'order' in node and TimeNode.model_validate(node).order != index:
                raise ValueError('节点列表已表示实际先后；冗余节点order与列表位置冲突')
            if any('node_id' in e and e['node_id'] != node['id'] for e in node['events']):
                raise ValueError('事件冗余node_id与所属分组冲突')
        return canonical
    return value


class RegisterCorrection(Record):
    entity_merges: list[EventMerge] = Field(default_factory=list)
    location_merges: list[EventMerge] = Field(default_factory=list)
    object_merges: list[EventMerge] = Field(default_factory=list)
    world_traits: dict[str, str] = Field(default_factory=dict)
    entities: dict[str, Entity] = Field(default_factory=dict)
    locations: dict[str, Location] = Field(default_factory=dict)
    objects: dict[str, Object] = Field(default_factory=dict)


class Correction(RegisterCorrection):
    events: list[Event] = Field(default_factory=list)
    event_updates: dict[str, dict] = Field(default_factory=dict,
        description='已有事件编号到需要修改的字段；未提交字段原样保留。新增事件用events，时间用time')
    merges: list[EventMerge] = Field(default_factory=list)
    removals: list[EventRemoval] = Field(default_factory=list)
    relations: list[Relation] | None = Field(default=None,
        description='增量新增/更新关系，不替换整表；迁移引用须同时用relation_removals显式删除旧边')
    changes: list[Change] | None = Field(default=None,
        description='增量新增/更新状态变化，不替换整表；迁移引用须同时用change_removals显式删除旧记录')
    relation_removals: list['RelationRemoval'] = Field(default_factory=list,
        description='显式删除旧关系，原样匹配当前source/target/kind；删除事件前须处理所有入边和出边')
    change_removals: list['ChangeRemoval'] = Field(default_factory=list,
        description='显式删除旧状态变化，原样匹配当前event_id/subject_id/change；删除事件前须处理其全部记录')

class RelationRemoval(Relation):
    reason: str = Field(min_length=1)


class ChangeRemoval(Change):
    reason: str = Field(min_length=1)


class CandidateCheck(Record):
    candidate_id: str
    event_ids: list[str]
    reason: str = Field(default='', description='仅在不收录或存在具体疑点时说明依据；正常映射无需逐项证明')
    disposition: Literal['mapped', 'merged', 'non_core', 'unsupported']
    # 历史工作区会携带这份重复抄写。当前协议由程序直接读取 reading
    # 候选的签名，不再让检查模型重抄一遍后逐字比较。
    signature: SkipJsonSchema[OccurrenceSignature | None] = Field(default=None, exclude=True)

    @model_validator(mode='before')
    @classmethod
    def legacy_check(cls, value):
        """旧Review仍能读取；当前签名由程序从reading候选取得。"""
        if isinstance(value, dict):
            text = str(value.get('reason') or '历史候选核对')
            value = dict(value)
            value.setdefault('disposition', 'mapped' if value.get('event_ids') else 'non_core')
            value.setdefault('signature', {'actual_subjects': [], 'occurrence': text,
                'start_anchor': '历史核对未结构化', 'layer': '未分类'})
        return value

    @field_validator('event_ids')
    @classmethod
    def unique_event_ids(cls, value):
        """重复编号不增加语义，也不能伪装成新的候选去向。"""
        return list(dict.fromkeys(value))


class TimeCheck(Record):
    id: str
    verdict: Literal['conflict', 'unknown'] | SkipJsonSchema[Literal['confirmed']]
    reason: str = Field(min_length=1, description='依据原文复核实际开始、节点归属及两层顺序；未解决时说明疑点')
    comparisons: dict[str, Literal['before', 'simultaneous', 'after', 'unknown']] = Field(
        default_factory=dict,
        description='只在发现具体冲突或确有歧义时，列出相关事件及实际开始关系；正确项无需逐对重交证明')
    # 格式36及以前的回复仍可读取，但活动协议不再要求为每一对正确
    # 关系重复提交理由和双方原文编号。事件本身的 Evidence 仍照常校验。
    comparison_reasons: SkipJsonSchema[dict[str, str]] = Field(default_factory=dict, exclude=True)
    comparison_sources: SkipJsonSchema[dict[str, list[Evidence]]] = Field(default_factory=dict, exclude=True)
    # 工作区格式35及以前的失败候选仍可只读解析；新公开协议只使用上面字段。
    confirmed: SkipJsonSchema[bool | None] = Field(default=None, exclude=True)
    anchor_evidence: SkipJsonSchema[list[Evidence]] = Field(default_factory=list, exclude=True)
    compared_to: SkipJsonSchema[list[str]] = Field(default_factory=list, exclude=True)
    uncertainty_evidence: SkipJsonSchema[list[Evidence]] = Field(default_factory=list, exclude=True)

    @model_validator(mode='before')
    @classmethod
    def legacy_verdict(cls, value):
        if isinstance(value, dict) and 'verdict' not in value and isinstance(value.get('confirmed'), bool):
            value = {**value, 'verdict': 'confirmed' if value['confirmed'] else 'unknown'}
        return value


class InformationAudit(Record):
    """记录检查过哪些资料类别；具体错误只写在 unresolved/patch。"""
    checked_sections: list[Literal[
        'characters', 'relations', 'locations', 'objects',
        'background', 'settings', 'story_overview', 'story_spine',
    ]] = Field(default_factory=list,
        description='一次列出已核对的资料类别；无需逐对象重复证明正确项')
    unresolved: list[str] = Field(default_factory=list)
    # 兼容旧工作区，活动 JSON 协议不再暴露逐对象确认清单。
    checked_entity_ids: SkipJsonSchema[list[str]] = Field(default_factory=list, exclude=True)
    checked_location_ids: SkipJsonSchema[list[str]] = Field(default_factory=list, exclude=True)
    relation_findings: SkipJsonSchema[dict[str, str]] = Field(default_factory=dict, exclude=True)
    background_checked: SkipJsonSchema[bool] = Field(default=False, exclude=True)
    settings_checked: SkipJsonSchema[bool] = Field(default=False, exclude=True)
    overview_checked: SkipJsonSchema[bool] = Field(default=False, exclude=True)

    @model_validator(mode='before')
    @classmethod
    def legacy_sections(cls, value):
        if not isinstance(value, dict) or 'checked_sections' in value:
            return value
        sections = []
        if 'checked_entity_ids' in value:
            sections.extend(('characters', 'objects'))
        if 'relation_findings' in value:
            sections.append('relations')
        if 'checked_location_ids' in value:
            sections.append('locations')
        if value.get('background_checked'):
            sections.append('background')
        if value.get('settings_checked'):
            sections.append('settings')
        if value.get('overview_checked'):
            sections.extend(('story_overview', 'story_spine'))
        return {**value, 'checked_sections': list(dict.fromkeys(sections))}


class CoreLinkCheck(Record):
    """按所在位置把一个故事主干环节绑定到一个实际事件。"""
    event_id: str = Field(min_length=1)
    # 历史回复可读；当前协议由程序按chain_index和列表位置直接取得
    # StoryLink及其签名，不让模型重抄文字、签名或正确项理由。
    link: SkipJsonSchema[str] = Field(default='', exclude=True)
    signature: SkipJsonSchema[OccurrenceSignature | None] = Field(default=None, exclude=True)
    reason: SkipJsonSchema[str] = Field(default='', exclude=True)


class StoryLink(Record):
    """故事主干中的一个可核对发生，而不是自由文本摘要。"""
    link: str = Field(min_length=1)
    signature: OccurrenceSignature
    core_reason: str = Field(min_length=1,
        description='改变或删除该发生会具体改变的因果链、转折、人物命运或结局')


class CoreChainCheck(Record):
    """对一个故事主干链逐环节核对；链文字由全文资料给出。"""
    chain_index: int = Field(ge=1)
    links: list[CoreLinkCheck] = Field(default_factory=list,
        description='按causal_chains对应链的位置逐项绑定一个event_id，顺序与链完全一致')
    missing_links: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    event_ids: SkipJsonSchema[list[str]] = Field(default_factory=list, exclude=True)

    @model_validator(mode='before')
    @classmethod
    def legacy_event_ids(cls, value):
        """格式36以前/迁移中的event_ids可读，但当前Workflow不会把占位映射当完成。"""
        if isinstance(value, dict) and 'links' not in value and value.get('event_ids'):
            value = {**value, 'links': [
                {'link': f'历史链环节{index}', 'event_id': event_id,
                 'signature': None, 'reason': '历史映射未结构化'}
                for index, event_id in enumerate(value['event_ids'], 1)]}
        return value


class StorySpine(Record):
    """按实际故事时间表达的结局、转折与最小充分因果链。"""
    endings: list[Annotated[str, StringConstraints(min_length=1)]] = Field(min_length=1)
    turning_points: list[Annotated[str, StringConstraints(min_length=1)]] = Field(min_length=1)
    causal_chains: list[list[StoryLink]] = Field(min_length=1)

    @model_validator(mode='before')
    @classmethod
    def legacy_text_links(cls, value):
        """历史档案仍可只读；当前质量门拒绝未结构化的主干环节。"""
        if not isinstance(value, dict):
            return value
        chains = value.get('causal_chains')
        if not isinstance(chains, list):
            return value
        normalized = []
        for chain in chains:
            if not isinstance(chain, list):
                normalized.append(chain)
                continue
            normalized.append([{
                'link': link,
                'signature': {'actual_subjects': [], 'occurrence': str(link),
                              'start_anchor': '历史主干未结构化', 'layer': '未分类'},
                'core_reason': '历史主干未结构化',
            } if isinstance(link, str) else link for link in chain])
        return {**value, 'causal_chains': normalized}

class TimelinePlan(Record):
    """时间任务只安排位置和相关边，不承担档案重写或自行确认。"""
    summary: str = Field(min_length=1, description='按故事实际发生顺序概括阶段链及关键跨块锚点')
    time: GroupedTimeline | SkipJsonSchema[Timeline | None] = Field(
        description='覆盖全部现存事件的完整分组；节点列表从早到晚，每个事件提交正整数节点内order')
    relations: list[Relation] = Field(default_factory=list)
    relation_removals: list[RelationRemoval] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list, description='原文仍无法解决的具体时间或事实疑点')

    _time_delivery = field_validator('time', mode='before')(time_delivery)


class Review(Record):
    is_valid: bool
    summary: str = Field(min_length=1)
    issues: list[str]
    time_valid: bool = Field(default=False,
        description='已依据全文核对故事实际开始顺序、节点归属及节点内顺序；具体错误写issues并在time修订')
    correction: Correction = Field(default_factory=Correction)
    time: GroupedTimeline | SkipJsonSchema[Timeline] | None = Field(default=None,
        description='需要改动时提交完整嵌套时间线；不改动用null。节点列表从早到晚，事件在各自节点内排序')
    time_checks: SkipJsonSchema[list[TimeCheck]] = Field(default_factory=list, exclude=True)
    covered_chunks: list[int]
    candidate_checks: SkipJsonSchema[list[CandidateCheck]] = Field(default_factory=list, exclude=True)
    information_audit: SkipJsonSchema[InformationAudit | None] = Field(default=None, exclude=True)
    core_chain_checks: SkipJsonSchema[list[CoreChainCheck]] = Field(default_factory=list, exclude=True)
    information_checked: SkipJsonSchema[bool] = Field(default=False, exclude=True)
    # 仅在人物等资料有误或实体目录补充时提交完整资料；协议由调用方提供。
    information: dict | None = None
    resolved_jobs: SkipJsonSchema[list[str]] = Field(default_factory=list, exclude=True)

    _time_delivery = field_validator('time', mode='before')(time_delivery)


def source_quote(quote, source):
    """只恢复唯一匹配片段的空白，保留原文字词、标点及来源位置。"""
    if quote in source:
        return quote
    needle = re.sub(r'\s+', '', quote)
    positions = [i for i, char in enumerate(source) if not char.isspace()]
    compact = ''.join(source[i] for i in positions)
    start = compact.find(needle) if needle else -1
    if start < 0 or compact.find(needle, start + 1) >= 0:
        return None
    return source[positions[start]:positions[start + len(needle) - 1] + 1]


def normalize_evidence(events, chunks):
    """原始回复另行保存；接收对象中的引文使用精确原文，无法定位的仍交校验。"""
    for event in evidence_records(events):
        resolved = []
        for index, evidence in enumerate(event.evidence):
            path = f'{getattr(event, "id", "candidate")}.evidence[{index}]'
            if evidence.passage_ids:
                if not 1 <= evidence.chunk <= len(chunks):
                    raise ValueError(path + ': 证据须引用有效块')
                passages = chunks[evidence.chunk - 1].passages()
                selected = []
                for key in evidence.passage_ids:
                    if key not in passages:
                        raise ValueError(f'{path}: 未知原文片段 {key}；仅使用所给第{evidence.chunk}块片段编号')
                    selected.append(passages[key])
                # 来源编号可定位不代表事实正确。错误复抄不丢整批事实，但必须在最终核实前解决。
                mismatch = evidence.unverified_quote
                if evidence.quote and not any(source_quote(evidence.quote, text) for text in selected):
                    mismatch = evidence.quote
                resolved.extend(Evidence(chunk=evidence.chunk, quote=text, passage_ids=[key], unverified_quote=mismatch)
                                for key, text in zip(evidence.passage_ids, selected))
                continue
            if 1 <= evidence.chunk <= len(chunks):
                quote = source_quote(evidence.quote, chunks[evidence.chunk - 1].text)
                if quote is not None:
                    evidence.quote = quote
            resolved.append(evidence)
        event.evidence = resolved


def evidence_records(value):
    """统一遍历事件、物品、状态及其delta的来源，不复制证据处理逻辑。"""
    if isinstance(value, BaseModel):
        if hasattr(value, 'evidence'):
            yield value
        for name in type(value).model_fields:
            if name != 'evidence':
                yield from evidence_records(getattr(value, name))
    elif isinstance(value, dict):
        for item in value.values():
            yield from evidence_records(item)
    elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from evidence_records(item)


def evidence_errors(records, chunks):
    errors = []
    for record in evidence_records(records):
        for evidence in record.evidence:
            if not evidence.quote or not 1 <= evidence.chunk <= len(chunks) or evidence.quote not in chunks[evidence.chunk - 1].text:
                errors.append(f'第{evidence.chunk}块证据不是连续原文：{evidence.quote[:80]!r}；'
                              '请选择 passages 中的编号；不连续对话用多个 passage_ids，不要拼接或改写')
    return errors


def canonical_id(key, rows):
    """只解析唯一的精确姓名/别称；相似称谓、未知对象和歧义不做猜测。"""
    if key in rows or key is None:
        return key
    matches = [identity for identity, item in rows.items() if key in [item.name, *item.aliases]]
    return matches[0] if len(matches) == 1 else key


def canonical_keys(rows, register):
    result = {}
    for key, value in rows.items():
        target = canonical_id(key, register)
        if target in result and result[target] != value:
            raise ValueError('同一对象提交了冲突资料：' + target)
        result[target] = value
    return result


def canonical_signature(signature, entities, redirects=None):
    """只规范化签名主体；发生、开始锚点和层次保持逐字稳定。"""
    if signature is None:
        return None
    redirects = redirects or {}
    def resolve(key):
        seen = set()
        while key in redirects:
            if key in seen:
                raise ValueError('人物合并重定向构成循环：' + key)
            seen.add(key)
            key = redirects[key]
        return canonical_id(key, entities)
    value = signature.model_copy(deep=True)
    value.actual_subjects = sorted(set(resolve(key) for key in value.actual_subjects))
    return value


def normalize_references(topology, register):
    for event in topology.events:
        for field in ('actor_ids', 'target_ids'):
            setattr(event, field, list(dict.fromkeys(canonical_id(key, register.entities) for key in getattr(event, field))))
        event.goals = canonical_keys(event.goals, register.entities)
        event.at_location_id = canonical_id(event.at_location_id, register.locations)
        event.object_ids = [canonical_id(key, register.objects) for key in event.object_ids]
    subjects = {**register.entities, **register.locations, **register.objects}
    for change in topology.changes:
        change.subject_id = canonical_id(change.subject_id, subjects)


def register_errors(register):
    errors = []
    seen = set()
    for group, prefix in (('entities', 'ENT_'), ('locations', 'LOC_'), ('objects', 'OBJ_')):
        for key in getattr(register, group):
            if not key.startswith(prefix) or key in seen:
                errors.append(f'{group}.{key}: 编号须以 {prefix} 开头且全局唯一')
            seen.add(key)
    for key, item in register.objects.items():
        if item.location_id is not None and item.location_id not in register.locations:
            errors.append(f'objects.{key}.location_id: 未知地点 {item.location_id}')
    return errors


def topology_errors(topology, register, chunks, check_time=False):
    """引用、引文与开始顺序检查；不猜测主体、同一性或缺失时间。"""
    errors, ids = [], set()
    people, locations, objects = set(register.entities), set(register.locations), set(register.objects)
    for event in topology.events:
        if event.id in ids:
            errors.append(f'events.{event.id}: 重复编号')
        ids.add(event.id)
        missing = set(event.actor_ids + event.target_ids) - people
        if missing:
            errors.append(f'events.{event.id}: 未知人物 {sorted(missing)}')
        if set(event.goals) - set(event.actor_ids + event.target_ids):
            errors.append(f'events.{event.id}.goals 的键必须是参与者编号 {event.actor_ids + event.target_ids}，'
                          f'不是目的名称 {sorted(set(event.goals) - set(event.actor_ids + event.target_ids))}；'
                          '格式为 {人物编号:目的描述}，未知目的用空对象，不删除事件')
        if event.at_location_id is not None and event.at_location_id not in locations:
            errors.append(f'events.{event.id}: 未知地点 {event.at_location_id}')
        if set(event.object_ids) - objects:
            errors.append(f'events.{event.id}: 未知物品 {sorted(set(event.object_ids) - objects)}')
        signature = canonical_signature(event.occurrence_signature, register.entities)
        if signature is None or signature.layer == '未分类':
            errors.append(f'events.{event.id}: 缺少明确occurrence_signature')
        elif signature.layer != event.occurrence_layer:
            errors.append(f'events.{event.id}: occurrence_signature.layer与occurrence_layer不一致')
        elif set(signature.actual_subjects) != set(event.actor_ids):
            errors.append(f'events.{event.id}: occurrence_signature.actual_subjects须与actor_ids一致；'
                          '实际主体不能由转述者、获知者或同场人物代替')
        errors.extend(f'events.{event.id}: {error}' for error in evidence_errors([event], chunks))
    if not check_time:
        for relation in topology.relations:
            if relation.source not in ids or relation.target not in ids or relation.source == relation.target:
                errors.append(f'relations: 非法事件引用 {relation.source} → {relation.target}')
    for change in topology.changes:
        if change.event_id not in ids or change.subject_id not in people | locations | objects | set(register.world_traits):
            errors.append(f'changes: 非法引用 {change.event_id}/{change.subject_id}')
    if check_time:
        errors.extend(temporal_errors(topology))
    from common.novel_extract.NovelExtractState import asset_errors
    return errors + asset_errors(topology, register, chunks)


def temporal_errors(topology):
    """仅校验显式开始关系、先后矛盾和环，不以无关事实/档案缺项阻塞时间草稿。"""
    errors = []
    events = {e.id: e for e in topology.events}
    nodes = {n.id: n for n in getattr(topology, 'time_nodes', [])}
    ids = set(events)
    for relation in topology.relations:
        if relation.source not in ids or relation.target not in ids or relation.source == relation.target:
            errors.append(f'relations: 非法事件引用 {relation.source} → {relation.target}')
        else:
            left_event, right_event = events[relation.source], events[relation.target]
            if (relation.kind == 'simultaneous' and left_event.time_node_id in nodes
                    and right_event.time_node_id in nodes and left_event.time_node_id != right_event.time_node_id):
                errors.append(f'relations: 同时事件 {relation.source}/{relation.target} 不能属于不同时间节点')
            comparison = compare_starts(events[relation.source], events[relation.target], nodes)
            if comparison is not None:
                if (relation.kind == 'before' and comparison >= 0 or
                        relation.kind in ('causes', 'enables', 'prevents') and comparison > 0):
                    positions = [(e.id, e.time_node_id, nodes[e.time_node_id].order, e.event_order)
                                 for e in (left_event, right_event)]
                    errors.append(f'relations: {relation.source} {relation.kind} {relation.target} 与开始顺序矛盾；'
                                  f'(事件,节点,节点order,事件order)={positions}；依据原文修正位置或错误边')
                if relation.kind == 'simultaneous' and comparison != 0:
                    errors.append(f'relations: {relation.source} 与 {relation.target} 同时开始却位置不同')
    # 即便某些时间未知，也不能接受显式先后关系构成的环。
    groups = {key: key for key in ids}
    def root(key):
        while groups[key] != key:
            key = groups[key]
        return key
    for relation in topology.relations:
        if relation.kind == 'simultaneous' and relation.source in ids and relation.target in ids:
            groups[root(relation.source)] = root(relation.target)
    edges = {root(key): set() for key in ids}
    for relation in topology.relations:
        if relation.kind == 'before' and relation.source in ids and relation.target in ids:
            edges[root(relation.source)].add(root(relation.target))
    degrees = {key: 0 for key in edges}
    for targets in edges.values():
        for key in targets:
            degrees[key] += 1
    queue = [key for key, count in degrees.items() if not count]
    visited = 0
    while queue:
        key = queue.pop()
        visited += 1
        for target in edges[key]:
            degrees[target] -= 1
            if degrees[target] == 0:
                queue.append(target)
    if visited != len(edges):
        errors.append('relations: 开始时间的先后/同时关系构成循环')
    return errors

def compare_starts(left, right, nodes):
    """先比较节点顺序，同一节点内再比较事件顺序；不同节点的事件序号不能直接比较。"""
    if left.time_node_id not in nodes or right.time_node_id not in nodes:
        return None
    if left.time_node_id == right.time_node_id:
        a, b = left.event_order, right.event_order
    else:
        a, b = nodes[left.time_node_id].order, nodes[right.time_node_id].order
    return None if a is None or b is None else (a > b) - (a < b)


def timeline_errors(topology, require_complete=False):
    """节点/事件的两层结构；块内使用局部顺序，跨块位置必须由模型重新比较。"""
    nodes = {node.id: node for node in topology.time_nodes}
    orders = [node.order for node in nodes.values() if node.order is not None]
    errors = []
    if require_complete and not topology.events:
        errors.append('尚未提取任何核心事件，不能确认完成；请依据原文和故事主干补齐实际发生及时间归属，不得编造')
    if len(nodes) != len(topology.time_nodes):
        errors.append('time_nodes 节点编号重复')
    if len(orders) != len(set(orders)):
        conflicts = {order: [n.id for n in nodes.values() if n.order == order]
                     for order in set(orders) if orders.count(order) > 1}
        errors.append(f'nodes.order 不得重号；冲突为 {conflicts}；重新比较这些节点，不要改成未知以规避冲突')
    if len(orders) == len(nodes) and orders and sorted(set(orders)) != list(range(1, len(set(orders)) + 1)):
        errors.append(f'nodes.order 须从1连续编号；当前为 {sorted(set(orders))}')
    for event in topology.events:
        if event.time_node_id not in nodes:
            errors.append(f'{event.id} 缺少有效时间节点 {event.time_node_id}；未知顺序可用null，但仍须归属节点')
        elif require_complete and event.event_order is None:
            errors.append(f'{event.id} 缺少节点内开始顺序')
    if require_complete:
        missing_node_orders = sorted(node.id for node in topology.time_nodes if node.order is None)
        if missing_node_orders:
            errors.append(f'时间节点缺少实际开始顺序：{missing_node_orders}')
    for node_id in nodes:
        node_events = [event for event in topology.events if event.time_node_id == node_id]
        event_orders = {event.event_order for event in node_events if event.event_order is not None}
        if (node_events and all(event.event_order is not None for event in node_events)
                and sorted(event_orders) != list(range(1, len(event_orders) + 1))):
            errors.append(f'{node_id}内events.order须从1连续编号；当前为 {sorted(event_orders)}')
    unused = nodes.keys() - {event.time_node_id for event in topology.events}
    if unused:
        errors.append(f'存在空时间节点 {sorted(unused)}')
    return errors


def event_time_content(event):
    """时间依赖实际发生、身份、地点和来源；收录理由、目标说明不撤销时间。"""
    data = event.model_dump(exclude={'time_node_id', 'event_order', 'temporal_basis',
                                     'core_reason', 'goals', 'required_results', 'state_delta'})
    for evidence in data['evidence']:
        evidence.pop('unverified_quote', None)
        if evidence['quote']:
            # 引文定位后，补留片段编号不改变其实际时间依据，兼容旧确认哈希。
            evidence['passage_ids'] = []
    return data


def time_dependencies(world):
    """确认绑定实际发生、原文锚点、位置与身份；目录/节点说明不是新的时序事实。"""
    events = {e.id: e for e in world.events}
    nodes = {n.id: n for n in world.time_nodes}
    node_id_counts = Counter(node.id for node in world.time_nodes)
    ambiguous_nodes = {node_id for node_id, count in node_id_counts.items() if count > 1}
    # 重复事件编号无法区分依赖版本；其他结构问题按受影响事件局部失效，
    # 不能因为一个待修位置清空整篇已经独立核实的时间。
    if len(events) != len(world.events):
        return {}
    comparisons = time_comparisons(world)
    inverse = {'before': 'after', 'after': 'before',
               'simultaneous': 'simultaneous', 'unknown': 'unknown'}
    result = {}
    for key, event in events.items():
        if event.time_node_id not in nodes or event.time_node_id in ambiguous_nodes:
            continue
        incident = []
        comparison_peers = set()
        for row in comparisons:
            if row['left'] == key:
                comparison_peers.add(row['right'])
                incident.append({'other': row['right'], 'scope': row['scope'], 'draft': row['draft']})
            elif row['right'] == key:
                comparison_peers.add(row['left'])
                incident.append({'other': row['left'], 'scope': row['scope'],
                                 'draft': inverse[row['draft']]})
        # 时间确认只绑定本事件和传递链中的直接比较端点。同节点的第三件事若没有
        # 与本事件形成比较边，其说明改写不应撤销这里已经核实的结论。
        related = {key} | comparison_peers
        relations = [r.model_dump(exclude={'basis'}, mode='json') for r in world.relations if key in (r.source, r.target)]
        for relation in relations:
            related.update((relation['source'], relation['target']))
        members = [events[k] for k in sorted(related)]
        if any(member.time_node_id in ambiguous_nodes for member in members):
            continue
        if any(q.unverified_quote for e in members for q in e.evidence):
            continue
        references = {'entities': {k for e in members for k in e.actor_ids + e.target_ids},
                      'locations': {e.at_location_id for e in members if e.at_location_id},
                      'objects': {k for e in members for k in e.object_ids}}
        if any(keys - getattr(world, field).keys() for field, keys in references.items()):
            continue
        result[key] = {
            'events': {e.id: {**event_time_content(e), 'node': nodes[e.time_node_id].model_dump(include={'id', 'order'}, mode='json')
                             if e.time_node_id in nodes else None,
                             'order': e.event_order} for e in members},
            'relations': sorted(relations, key=lambda r: (r['source'], r['target'], r['kind'])),
            # 这些比较边仅绑定确认版本，不要求模型逐对交付证明。
            # 任一端的发生依据或邻接变化，只撤销受影响边界的确认。
            'comparisons': sorted(incident, key=lambda row: (row['other'], row['scope'], row['draft'])),
            **{field: {k: getattr(world, field)[k].model_dump(
                exclude={'description', 'function', 'significance', 'evidence'}, mode='json') for k in sorted(keys)}
               for field, keys in references.items()}}
    return result


def fact_dependencies(world, chunks):
    """逐块绑定已核对的事实；事实变化重新检查相关来源，时间重排不重启全文查漏。"""
    result = {}
    for chunk in chunks:
        events = [e for e in world.events if any(q.chunk == chunk.index for q in e.evidence)]
        ids = {e.id for e in events}
        relations = [r for r in world.relations if ids & {r.source, r.target}]
        related = ids | {key for r in relations for key in (r.source, r.target)}
        members = [e for e in world.events if e.id in related]
        references = {'entities': {k for e in members for k in e.actor_ids + e.target_ids},
                      'locations': {e.at_location_id for e in members if e.at_location_id},
                      'objects': {k for e in members for k in e.object_ids}}
        changes = [c for c in world.changes if c.event_id in ids]
        trait_ids = {c.subject_id for c in changes if c.subject_id in world.world_traits}
        result[chunk.index] = {
            'events': {e.id: e.model_dump(exclude={'time_node_id', 'event_order', 'temporal_basis'}, mode='json')
                       for e in events},
            'related_events': {e.id: event_time_content(e) for e in members if e.id not in ids},
            'relations': [r.model_dump(mode='json') for r in relations],
            'changes': [c.model_dump(mode='json') for c in changes],
            # 世界规则只在本块事件的状态变化明确引用时绑定。无关规则改写不应让全文覆盖失效。
            'world_traits': {key: world.world_traits[key] for key in sorted(trait_ids)},
            **{field: {key: getattr(world, field)[key].model_dump(mode='json')
                       for key in sorted(keys) if key in getattr(world, field)} for field, keys in references.items()}}
    return result


def information_dependency(world, information):
    """目录和档案单独绑定版本，避免档案变化重启所有事件块核对。"""
    catalog = Register.model_validate(world.model_dump(mode='json')).model_dump(mode='json')
    details = information.model_dump(mode='json') if isinstance(information, BaseModel) else deepcopy(information)
    return {'catalog': catalog, 'information': details}


def time_comparisons(world):
    """用传递链覆盖两层顺序，避免把同一结论扩成二次方重复比较。"""
    groups, pairs = {}, []
    labels = {-1: 'before', 0: 'simultaneous', 1: 'after', None: 'unknown'}
    node_id_counts = Counter(node.id for node in world.time_nodes)
    nodes = [node for node in world.time_nodes if node_id_counts[node.id] == 1]
    for node in nodes:
        events = sorted((e for e in world.events if e.time_node_id == node.id), key=lambda e: e.id)
        by_order = {}
        for event in events:
            by_order.setdefault(event.event_order, []).append(event)
        ordered_groups = [by_order[key] for key in sorted(by_order, key=lambda key: (key is None, key or 0))]
        # 同序组是“同时开始”的等价类：一件代表事件与其余成员比较即可覆盖全组。
        # 相邻序组再用代表事件连接，借助传递性覆盖组内完整先后，不重复提交所有事件对。
        for same_start in ordered_groups:
            anchor = same_start[0]
            for peer in same_start[1:]:
                pairs.append({'left': anchor.id, 'right': peer.id, 'scope': 'within_node',
                              'draft': labels[compare_starts(anchor, peer, {node.id: node})]})
        for earlier, later in zip(ordered_groups, ordered_groups[1:]):
            left, right = earlier[0], later[0]
            pairs.append({'left': left.id, 'right': right.id, 'scope': 'within_node',
                          'draft': labels[compare_starts(left, right, {node.id: node})]})
        if events:
            groups[node.id] = ordered_groups
    # 只连接顺序明确、编号相邻的节点。某个节点待定、重号或缺号时，
    # 仅切断它附近的边界；远端连续区间仍保留原有确认依赖。
    order_counts = Counter(node.order for node in nodes if node.order is not None)
    ordered_nodes = sorted((node for node in nodes
                            if node.order is not None and order_counts[node.order] == 1
                            and node.id in groups), key=lambda node: node.order)
    for a, b in zip(ordered_nodes, ordered_nodes[1:]):
        if b.order != a.order + 1:
            continue
        left_groups, right_groups = groups[a.id], groups[b.id]
        # 组内链已核实每个成员的位置；只需比较前一节点最晚序组与后一节点
        # 最早序组的代表事件，就能证明两节点没有交错。
        earlier, later = left_groups[-1][0], right_groups[0][0]
        pairs.append({'left': earlier.id, 'right': later.id, 'scope': 'between_nodes',
                      'draft': labels[compare_starts(earlier, later, {a.id: a, b.id: b})]})
    return pairs


def apply_correction(world, review, chunks=(), pending=None):
    """兼容旧导入；新代码直接使用 NovelExtractRevision。"""
    from common.novel_extract.NovelExtractRevision import apply_correction as implementation
    return implementation(world, review, chunks, pending)


def update_records(current, updates, removals, fields, references):
    """兼容旧导入；新代码直接使用 NovelExtractRevision。"""
    from common.novel_extract.NovelExtractRevision import update_records as implementation
    return implementation(current, updates, removals, fields, references)


def apply_timeline(world, review, chunks, previous_nodes=()):
    """兼容旧导入；新代码直接使用 NovelExtractRevision。"""
    from common.novel_extract.NovelExtractRevision import apply_timeline as implementation
    return implementation(world, review, chunks, previous_nodes)


class CharacterInformation(Record):
    activity: Literal['主角', 'npc']
    nickname: str | None = None
    profile: str | None = None
    gender: str | None = None
    identity: list[str] | None = None
    age: str | None = None
    personality: str | None = None
    biography: str | None = None


class LocationInformation(Record):
    description: str | None = None
    detail: str | None = None


class BackgroundInformation(Record):
    world_name: str | None = None
    language: str | None = None
    description: str | None = None
    detail: str | None = None


class SettingInformation(Record):
    term: str = Field(min_length=1)
    nature: list[str] | None = None
    detail: list[str] | None = None


class RelationInformation(Record):
    owner: str
    other: str
    relation: list[Annotated[str, StringConstraints(min_length=1)]] = Field(min_length=1)
    detail: str | None = None


class RepositoryInformation(Record):
    story_overview: str = Field(description='全篇主要冲突、关键因果链、转折和结局；用于核心事件定位，不替代原文')
    story_spine: StorySpine
    characters: dict[str, CharacterInformation]
    locations: dict[str, LocationInformation]
    background: BackgroundInformation
    settings: list[SettingInformation] = Field(default_factory=list)
    relations: list[RelationInformation] = Field(default_factory=list)
    initial_state: AssetState | None = Field(default=None,
        description='故事实际起点之前的已知关系与物品状态；后期才成立的状态不得前置，缺乏依据保留未知')


class Foundation(Record):
    catalog: Register
    information: dict = Field(default_factory=dict,
        description='同次阅读全文形成的RepositoryInformation，结构见information_schema；档案问题单独修复，不丢失合法目录')

    @model_validator(mode='before')
    @classmethod
    def normalize_catalog_nesting(cls, value):
        """只归位唯一、无歧义的目录字段；原始回复仍由 calls 完整保留。"""
        if not isinstance(value, dict):
            return value
        fields = set(Register.model_fields)
        root_fields = fields & value.keys()
        catalog = value.get('catalog')
        if not root_fields or catalog is not None and not isinstance(catalog, dict):
            return value
        normalized = deepcopy(value)
        nested = dict(catalog or {})
        for field in sorted(root_fields):
            if field in nested and nested[field] != value[field]:
                raise ValueError(f'{field} 同时出现在根级和catalog且内容冲突，须核对后仅保留catalog.{field}')
            nested.setdefault(field, value[field])
            normalized.pop(field, None)
        normalized['catalog'] = nested
        return normalized


class FoundationReview(Record):
    correction: RegisterCorrection = Field(default_factory=RegisterCorrection)
    information: dict | None = Field(default=None, description='只提交新增或变化的档案字段；未提交保留')
    issues: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    audit: SkipJsonSchema[InformationAudit | None] = Field(default=None, exclude=True)


class FoundationFullReview(FoundationReview):
    """初提档案无效时，核对必须交回完整的已核实档案。"""
    information: RepositoryInformation


def apply_foundation_review(foundation, check):
    """兼容旧导入；新代码直接使用 NovelExtractRevision。"""
    from common.novel_extract.NovelExtractRevision import apply_foundation_review as implementation
    return implementation(foundation, check)


class InformationPatch(Record):
    characters: dict[str, CharacterInformation] = Field(default_factory=dict)
    locations: dict[str, LocationInformation] = Field(default_factory=dict)


def merge_bundle_gaps(correction, information):
    """兼容旧导入；新代码直接使用 NovelExtractRevision。"""
    from common.novel_extract.NovelExtractRevision import merge_bundle_gaps as implementation
    return implementation(correction, information)


def merge_bundle_errors(correction, information):
    """兼容旧导入；新代码直接使用 NovelExtractRevision。"""
    from common.novel_extract.NovelExtractRevision import merge_bundle_errors as implementation
    return implementation(correction, information)


def apply_information(current, review, register, complete=True):
    """兼容旧导入；新代码直接使用 NovelExtractRevision。"""
    from common.novel_extract.NovelExtractRevision import apply_information as implementation
    return implementation(current, review, register, complete)


def information_errors(information, register, complete=True):
    """资料须覆盖统一目录；未知内容填空，不靠程序补造。"""
    information.characters = canonical_keys(information.characters, register.entities)
    information.locations = canonical_keys(information.locations, register.locations)
    for relation in information.relations:
        relation.owner = canonical_id(relation.owner, register.entities)
        relation.other = canonical_id(relation.other, register.entities)
    errors = []
    for name, actual, expected in (
        ('characters', set(information.characters), set(register.entities)),
        ('locations', set(information.locations), set(register.locations)),
    ):
        if actual - expected or complete and actual != expected:
            errors.append(f'{name} 缺少 {sorted(expected - actual)}；未知编号 {sorted(actual - expected)}')
    for relation in information.relations:
        if relation.owner not in register.entities or relation.other not in register.entities or relation.owner == relation.other:
            errors.append(f'关系引用非法：{relation.owner} → {relation.other}')
    if information.initial_state is not None:
        from common.novel_extract.NovelExtractState import state_reference_errors
        errors.extend(state_reference_errors(information.initial_state, register, 'initial_state'))
    return errors
