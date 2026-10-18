"""Destructive review operations require evidence-bound, identity-safe input."""

import unittest

from common.novel_extract.NovelExtractReader import chunk_text
from common.novel_extract.NovelExtractSchema import (
    CandidateCheck,
    Change,
    ChangeRemoval,
    Correction,
    Entity,
    Event,
    EventMerge,
    EventRemoval,
    Evidence,
    OccurrenceSignature,
    Relation,
    RelationRemoval,
    Review,
    TimeCheck,
    TimeNode,
    World,
    apply_correction,
    evidence_errors,
    fact_dependencies,
    normalize_evidence,
)
from common.novel_extract.NovelExtractRuntime import digest
from workflows.NovelExtractWorkflow import NovelExtractWorkflow, ReviewState


def signature(subject="ENT_1", layer="实际发生"):
    return OccurrenceSignature(
        actual_subjects=[subject],
        occurrence="甲实际出发",
        start_anchor="故事开始",
        layer=layer,
    )


def event(key, actor="ENT_1", layer="实际发生", node=None, order=None, passage="C1_P1"):
    return Event(
        id=key,
        description=f"{key}实际发生",
        actor_ids=[actor],
        source_time="原文中的故事阶段",
        time_node_id=node,
        event_order=order,
        temporal_basis="原文开始时间线索",
        core_reason="改变此事会改变后续故事走向",
        evidence=[Evidence(chunk=1, passage_ids=[passage])],
        epistemic_status="事实",
        occurrence_layer=layer,
        occurrence_signature=signature(actor, layer),
    )


class StorageStub:
    def __init__(self, candidate, review_state=None):
        self.candidate = candidate
        self.review_state = review_state or {}

    def latest(self, key, default=None):
        if key == "job:chunk.1.reading":
            return {"result": {"core_candidates": [self.candidate]}}
        return default

    def get(self, key, default=None):
        return self.review_state if key == "review_state" else default


class SafetyContractTests(unittest.TestCase):
    def setUp(self):
        self.chunks = chunk_text("甲出发。\n乙到达。\n旁人评论。\n")
        self.world = World(
            entities={"ENT_1": Entity(name="甲"), "ENT_2": Entity(name="乙")},
            locations={},
            objects={},
            world_traits={},
            events=[event("A")],
        )
        self.reading_signature = signature()
        self.reading_candidate = {
            "description": "甲实际出发",
            "evidence": [{"chunk": 1, "passage_ids": ["C1_P1"]}],
            "signature": self.reading_signature.model_dump(mode="json"),
            "core_reason": "删除出发会使后续旅程无法发生",
        }

    def evaluate_removal(self, removal_signature, checks=(), retained=None):
        workflow = NovelExtractWorkflow()
        state = ReviewState(candidate_checks={"C1_K1": retained}).model_dump(mode="json") \
            if retained else {}
        workflow.storage = StorageStub(self.reading_candidate, state)
        workflow.failures = []
        review = Review(
            is_valid=False,
            summary="对已保存事件执行有证据的去留复核",
            issues=[],
            covered_chunks=[],
            candidate_checks=list(checks),
            correction=Correction(removals=[EventRemoval(
                id="A",
                reason="不属于核心事件",
                signature=removal_signature,
            )]),
        )
        candidate, _details = workflow.evaluate_review(review, self.world, self.chunks)
        return candidate, review

    def test_event_removal_binds_saved_identity(self):
        changed = self.reading_signature.model_copy(update={"start_anchor": "改写后的时间锚点"})
        mismatched_candidate, mismatched_review = self.evaluate_removal(changed)
        self.assertEqual([item.id for item in mismatched_candidate.events], ["A"])
        self.assertTrue(any("发生签名与已保存事件不一致" in issue for issue in mismatched_review.issues))

        accepted_candidate, accepted_review = self.evaluate_removal(self.reading_signature)
        self.assertEqual(accepted_candidate.events, [])
        self.assertFalse(any("事件删除 A" in issue for issue in accepted_review.issues))

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_duplicate_candidate_checks_do_not_change_saved_disposition(self):
        workflow = NovelExtractWorkflow()
        workflow.storage = StorageStub(self.reading_candidate)
        mapped = CandidateCheck(candidate_id="C1_K1", event_ids=["A"], reason="收录",
                                disposition="mapped")
        excluded = CandidateCheck(candidate_id="C1_K1", event_ids=[], reason="排除",
                                  disposition="non_core")
        review = Review(is_valid=False, summary="矛盾去向", issues=[], covered_chunks=[1],
                        candidate_checks=[mapped, excluded])
        state = ReviewState(checked_facts={1: digest(fact_dependencies(self.world, self.chunks)[1])})

        workflow.advance_fact_review(state, review, self.world, self.world, self.chunks)

        self.assertEqual(state.candidate_checks, {})

    def test_wrong_unbound_event_can_be_replaced_without_identity_deadlock(self):
        bad = event("A", actor="ENT_2", layer="获知")
        good = event("GOOD")
        world = self.world.model_copy(update={"events": [bad]})
        workflow = NovelExtractWorkflow()
        workflow.storage = StorageStub(self.reading_candidate)
        workflow.failures = []
        review = Review(
            is_valid=False, summary="用实际发生替换错误获知事件", issues=[], covered_chunks=[],
            candidate_checks=[CandidateCheck(
                candidate_id="C1_K1", event_ids=["GOOD"], reason="原候选对应实际出发",
                disposition="mapped")],
            correction=Correction(
                events=[good],
                removals=[EventRemoval(id="A", reason="错误获知事件", signature=bad.occurrence_signature)],
            ),
        )
        candidate, _ = workflow.evaluate_review(review, world, self.chunks)
        self.assertEqual([item.id for item in candidate.events], ["GOOD"])

    def test_agent_explicitly_repairs_references_when_replacing_an_event(self):
        bad = event("BAD", actor="ENT_2", layer="获知")
        good = event("GOOD")
        later = event("LATER", actor="ENT_2")
        old_relation = Relation(source="BAD", target="LATER", kind="causes", basis="旧因果")
        old_change = Change(event_id="BAD", subject_id="ENT_2", change="乙获知消息", basis="原文")
        world = self.world.model_copy(update={
            "events": [bad, later],
            "relations": [old_relation],
            "changes": [old_change],
        })
        removal = EventRemoval(id="BAD", reason="实际发生不能由获知替代",
                               signature=bad.occurrence_signature)
        incomplete = Review(
            is_valid=False, summary="以正确实际发生替代错误获知事件", issues=[], covered_chunks=[],
            correction=Correction(events=[good], removals=[removal]),
        )
        workflow = NovelExtractWorkflow()
        workflow.storage = StorageStub(self.reading_candidate)
        with self.assertRaisesRegex(ValueError, "非法事件引用|非法引用"):
            workflow.apply_review_correction(world, incomplete, self.chunks, None)

        review = Review(
            is_valid=False, summary="检查智能体逐条处理旧引用", issues=[], covered_chunks=[],
            correction=Correction(
                events=[good], removals=[removal],
                relation_removals=[RelationRemoval(**old_relation.model_dump(), reason="改正因果主体")],
                relations=[Relation(source="GOOD", target="LATER", kind="causes", basis="原文支持")],
                change_removals=[ChangeRemoval(**old_change.model_dump(), reason="旧获知状态不属于实际事件")],
                changes=[Change(event_id="GOOD", subject_id="ENT_1", change="甲开始新的经历", basis="原文")],
            ),
        )
        revised = workflow.apply_review_correction(world, review, self.chunks, None)
        self.assertEqual({item.id for item in revised.events}, {"GOOD", "LATER"})
        self.assertEqual(revised.relations[0].source, "GOOD")
        self.assertEqual(revised.relations[0].basis, "原文支持")
        self.assertEqual(revised.changes[0].event_id, "GOOD")
        self.assertEqual(revised.changes[0].change, "甲开始新的经历")
        self.assertEqual([item.id for item in world.events], ["BAD", "LATER"])

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_candidate_signature_survives_accepted_entity_redirect(self):
        old_signature = signature("ENT_C1_1")
        reading = dict(self.reading_candidate)
        reading["signature"] = old_signature.model_dump(mode="json")
        workflow = NovelExtractWorkflow()
        workflow.storage = StorageStub(reading)
        world = self.world.model_copy(deep=True)
        world.events[0].occurrence_signature = signature("ENT_1")
        check = CandidateCheck(candidate_id="C1_K1", event_ids=["A"], reason="合并后仍是同一主体",
                               disposition="mapped")
        self.assertFalse(workflow.candidate_check_errors(
            [check], world, self.chunks, {"ENT_C1_1": "ENT_1"}))

    def test_retired_entity_id_cannot_reappear_after_an_accepted_merge(self):
        workflow = NovelExtractWorkflow()
        workflow.storage = StorageStub(
            self.reading_candidate,
            ReviewState(entity_redirects={"ENT_OLD": "ENT_1"}).model_dump(mode="json"),
        )
        workflow.failures = []
        review = Review(
            is_valid=False, summary="错误复用已合并编号", issues=[], covered_chunks=[],
            correction=Correction(entities={"ENT_OLD": Entity(name="另一个人物")}),
        )

        candidate, _ = workflow.evaluate_review(review, self.world, self.chunks)

        self.assertNotIn("ENT_OLD", candidate.entities)
        self.assertTrue(any("已合并的人物编号不可重新使用" in issue for issue in review.issues))

    @unittest.skip("Legacy proof-table protocol retired; see reports/novel_extract/shadowloom_validation_reset_20261016/retired_tests.md")
    def test_time_review_uses_saved_event_evidence_without_pairwise_source_copies(self):
        world = self.world.model_copy(deep=True)
        world.events = [
            event("A", node="N1", order=1, passage="C1_P1"),
            event("B", actor="ENT_2", node="N2", order=1, passage="C1_P2"),
        ]
        world.time_nodes = [
            TimeNode(id="N1", name="出发", order=1),
            TimeNode(id="N2", name="到达", order=2),
        ]
        check = TimeCheck(
            id="A",
            verdict="confirmed",
            reason="已核对实际发生先后",
            comparisons={"B": "before"},
        )
        review = Review(
            is_valid=True,
            summary="使用无关来源确认时间",
            issues=[],
            covered_chunks=[1],
            time_checks=[check],
        )

        errors = NovelExtractWorkflow().validate_time_checks(
            review, world, self.chunks, {"A"})
        self.assertFalse(errors)

        normalized = [item.model_copy(deep=True) for item in world.events]
        normalize_evidence(normalized, self.chunks)
        self.assertFalse(evidence_errors(normalized, self.chunks))
        broken = event("BROKEN", passage="C1_MISSING")
        with self.assertRaisesRegex(ValueError, "未知原文片段"):
            normalize_evidence([broken], self.chunks)

    def test_event_merge_rejects_different_actor_layer_or_known_start(self):
        cases = [
            (
                "actor",
                event("SOURCE", actor="ENT_1"),
                event("TARGET", actor="ENT_2"),
                [],
                "四项发生签名不同",
            ),
            (
                "layer",
                event("SOURCE", layer="实际发生"),
                event("TARGET", layer="讲述"),
                [],
                "四项发生签名不同",
            ),
            (
                "start",
                event("SOURCE", node="N1", order=1),
                event("TARGET", node="N2", order=1),
                [TimeNode(id="N1", name="早年", order=1),
                 TimeNode(id="N2", name="晚年", order=2)],
                "位于不同实际开始位置",
            ),
        ]
        for label, source, target, nodes, message in cases:
            with self.subTest(label=label):
                world = World(
                    entities=self.world.entities,
                    locations={},
                    objects={},
                    world_traits={},
                    events=[source, target],
                    time_nodes=nodes,
                )
                review = Review(
                    is_valid=False,
                    summary="尝试合并事件",
                    issues=[],
                    covered_chunks=[],
                    correction=Correction(
                        events=[target.model_copy(deep=True)],
                        merges=[EventMerge(source="SOURCE", target="TARGET", reason="声称为同一发生")],
                    ),
                )
                with self.assertRaisesRegex(ValueError, message):
                    apply_correction(world, review)

    def test_event_merge_and_removal_require_the_exact_saved_occurrence(self):
        source = event("SOURCE")
        target = event("TARGET")
        target.occurrence_signature = target.occurrence_signature.model_copy(
            update={"occurrence": "甲完成捐献"})
        world = self.world.model_copy(update={"events": [source, target]})
        rewritten_target = target.model_copy(deep=True)
        rewritten_target.occurrence_signature = source.occurrence_signature.model_copy(deep=True)
        review = Review(
            is_valid=False, summary="不能合并不同发生", issues=[], covered_chunks=[],
            correction=Correction(
                events=[rewritten_target],
                merges=[EventMerge(source="SOURCE", target="TARGET", reason="主体相同但发生不同")],
            ),
        )
        with self.assertRaisesRegex(ValueError, "occurrence_signature是稳定发生身份"):
            apply_correction(world, review)

        wrong_candidate = self.reading_signature.model_copy(update={"occurrence": "另一件事"})
        kept, checked = self.evaluate_removal(wrong_candidate)
        self.assertEqual([item.id for item in kept.events], ["A"])
        self.assertTrue(checked.issues)

        other = event("A")
        other.occurrence_signature = signature().model_copy(update={"occurrence": "甲完成捐献"})
        delete_other = Review(
            is_valid=False, summary="候选不能删除另一件事", issues=[], covered_chunks=[],
            candidate_checks=[CandidateCheck(
                candidate_id="C1_K1", event_ids=[], reason="不收录出发",
                disposition="non_core")],
            correction=Correction(removals=[EventRemoval(
                id="A", reason="误删另一事件", signature=self.reading_signature)]),
        )
        with self.assertRaisesRegex(ValueError, "与已保存事件不一致"):
            apply_correction(self.world.model_copy(update={"events": [other]}), delete_other)

    def test_same_event_id_cannot_be_reused_for_another_occurrence(self):
        changed = self.world.events[0].model_copy(deep=True)
        changed.occurrence_signature = changed.occurrence_signature.model_copy(
            update={"occurrence": "甲完成捐献"})
        review = Review(
            is_valid=False, summary="错误复用编号", issues=[], covered_chunks=[],
            correction=Correction(events=[changed]),
        )
        with self.assertRaisesRegex(ValueError, "稳定发生身份"):
            apply_correction(self.world, review)


if __name__ == "__main__":
    unittest.main()
