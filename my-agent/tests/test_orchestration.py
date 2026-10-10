"""The orchestration against mock workers: parallel workers, evidence -> event -> dependency -> task, no duplicate
tasks, stale results on updated facts, invalid evidence kept out, a failing worker not blocking the others, and
the demo end to end. All values are invented placeholders."""
import asyncio
import io
import json
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestration import (DependencyGraph, EventBus, EventLog, EventType, EvidenceStore, FactStatus,  # noqa: E402
                           MockWorker, Needs, Orchestrator, Provenance, Scheduler, TaskSpec, TaskStatus,
                           default_graph, demo, validation)
from orchestration.scheduler import dedup_key  # noqa: E402

TENDER_AGENT = Provenance(site="tender", agent="tender", task_id="t1")
SPEC_AGENT = Provenance(site="spec", agent="spec", task_id="s1")
ERP_AGENT = Provenance(site="erp", agent="erp", task_id="e1")


def small_graph() -> DependencyGraph:
    """Two roots and one dependent task: enough to watch the scheduler without the whole default graph."""
    return DependencyGraph([
        TaskSpec("discover_tender", "tender", "find the tender", produces=["tender", "position"],
                 required_keys=["tender"]),
        TaskSpec("read_specification", "spec", "read the spec", produces=["requirement"]),
        TaskSpec("search_mails", "mail", "read the mailbox", produces=["mail"]),
        TaskSpec("check_discount_approval", "mail", "approvals for the confirmed reference",
                 requires=[Needs("tender")], produces=["approval"],
                 fan_out=lambda s, tasks: [{"reference": v["reference"]} for v in s.values("tender")],
                 context=lambda s, p: [f.key for f in s.read(kind="tender")]),
        TaskSpec("find_materials", "erp", "materials for a requirement",
                 requires=[Needs("requirement")], produces=["material"],
                 fan_out=lambda s, tasks: [{"requirement_id": v["id"]} for v in s.values("requirement")],
                 context=lambda s, p: [f.key for f in s.read(kind="requirement") if f.value["id"] == p["requirement_id"]]),
    ])


class Harness:
    def __init__(self, graph: DependencyGraph | None = None, deadline: float | None = 5.0):
        self.log = EventLog(None, echo=False)
        self.bus = EventBus()
        self.store = EvidenceStore(self.bus, self.log)
        self.scheduler = Scheduler(graph or small_graph(), self.store, self.log)
        self.workers: dict[str, MockWorker] = {}
        self.deadline = deadline

    def worker(self, name: str, handler, delay: float = 0.0) -> MockWorker:
        self.workers[name] = MockWorker(name, handler, self.bus, self.log, delay=delay)
        return self.workers[name]

    async def run(self) -> dict:
        deadline = None if self.deadline is None else time.monotonic() + self.deadline
        return await Orchestrator(self.store, self.scheduler, self.workers, self.log, deadline, stop_grace=1).run()

    def logged(self, name: str) -> list[dict]:
        return [r for r in self.log.records if r["event"] == name]


def nothing(task, context):
    return None


class TheEvidenceStore(unittest.IsolatedAsyncioTestCase):
    async def test_a_published_record_is_validated_and_raises_one_event(self):
        store = EvidenceStore()
        result = await store.publish("tender", {"reference": "REF-X", "title": "t"}, TENDER_AGENT)
        self.assertTrue(result.accepted)
        self.assertEqual(result.fact.key, "tender:REF-X")
        self.assertEqual(result.fact.version, 1)
        self.assertEqual([e.type for e in store.bus.history], [EventType.TENDER_DISCOVERED])
        self.assertEqual(store.values("tender"), [{"reference": "REF-X", "title": "t"}])

    async def test_the_same_fact_again_raises_no_event_and_no_version(self):
        store = EvidenceStore()
        await store.publish("tender", {"reference": "REF-X", "title": "t"}, TENDER_AGENT)
        again = await store.publish("tender", {"Referenz": "REF-X", "title": "t"}, TENDER_AGENT)  # alias spelling
        self.assertFalse(again.changed)
        self.assertIsNone(again.event)
        self.assertEqual(again.fact.version, 1)
        self.assertEqual(len(store.bus.history), 1)

    async def test_an_updated_fact_gets_a_new_version_and_invalidates_the_old_one(self):
        store = EvidenceStore()
        await store.publish("position", {"number": "1", "quantity": "10"}, TENDER_AGENT)
        update = await store.publish("position", {"number": "1", "quantity": "12"}, TENDER_AGENT)
        self.assertTrue(update.changed)
        self.assertEqual(update.fact.version, 2)
        self.assertEqual([e.type for e in store.bus.history],
                         [EventType.POSITIONS_FOUND, EventType.EVIDENCE_INVALIDATED, EventType.POSITIONS_FOUND])
        self.assertEqual([f.status for f in store.history("position:1")],
                         [FactStatus.INVALIDATED, FactStatus.VALIDATED])
        self.assertEqual(store.values("position"), [{"number": "1", "quantity": "12"}])

    async def test_invalid_evidence_is_not_validated_and_raises_nothing(self):
        store = EvidenceStore()
        result = await store.publish("material", {"description": "no number"}, ERP_AGENT)
        self.assertFalse(result.accepted)
        self.assertEqual(result.fact.status, FactStatus.PROPOSED)
        self.assertIn("material_number", result.fact.issues[0])
        self.assertEqual(store.values("material"), [])
        self.assertEqual(store.bus.history, [])
        self.assertEqual(store.snapshot()["erp"]["_rejected"][0]["raw"], {"description": "no number"})

    async def test_a_contradiction_from_another_source_is_kept_as_conflicting_not_validated(self):
        store = EvidenceStore()
        await store.publish("requirement", {"id": "R-1", "bore": "a"}, SPEC_AGENT)
        other = await store.publish("requirement", {"id": "R-1", "bore": "b"}, TENDER_AGENT)
        self.assertEqual(other.fact.status, FactStatus.CONFLICTING)
        self.assertEqual(other.fact.conflicts_with, "requirement:R-1")
        self.assertEqual(store.values("requirement"), [{"id": "R-1", "bore": "a"}])
        self.assertEqual(len(store.read(kind="requirement", status=FactStatus.CONFLICTING)), 1)
        self.assertIn("_conflict", store.snapshot()["tender"]["requirements"][0])

    async def test_notes_and_documents_in_the_shapes_the_agents_use_are_accepted(self):
        store = EvidenceStore()
        results = await store.publish_facts({"document": {"dokumentnummer": "D-1", "titel": "a title"},
                                             "notes": {"page_3": "unclear", "seen": ["x"]}}, SPEC_AGENT)
        self.assertTrue(all(r.accepted for r in results), [r.fact.issues for r in results])
        snapshot = store.snapshot()["spec"]
        self.assertIn(snapshot["document"]["name"], ("D-1", "a title"))
        self.assertEqual(set(snapshot["document"].values()), {"D-1", "a title"})   # nothing lost
        self.assertEqual(snapshot["notes"], {"page_3": "unclear", "seen": ["x"]})

    async def test_lists_saved_as_a_mapping_and_odd_identifier_names_are_understood(self):
        store = EvidenceStore()
        results = await store.publish_facts({
            "requirements": {"A-1": {"kataloggruppe": "g1", "bohrung": "x"}, "A-2": {"kataloggruppe": "g2"}},
            "materials": [{"artikel_code": "M-7", "status": "s"}],
            "positions": [{"pos_id": "3", "tender_reference": "REF-X"}],
        }, SPEC_AGENT)
        self.assertTrue(all(r.accepted for r in results), [r.fact.issues for r in results])
        self.assertEqual(sorted(r.fact.key for r in results),
                         ["material:M-7", "position:3", "requirement:A-1", "requirement:A-2"])
        self.assertEqual(store.get("position:3").value["_identity_from"], "pos_id")

    async def test_unknown_information_is_kept_not_dropped(self):
        store = EvidenceStore()
        results = await store.publish_facts({"tender": {"reference": "REF-X"}, "surprises": ["something new"],
                                             "notes": "ambiguous page"}, TENDER_AGENT)
        kinds = {r.fact.kind for r in results}
        self.assertEqual(kinds, {"tender", "unknown", "note"})
        snapshot = store.snapshot()["tender"]
        self.assertEqual(snapshot["surprises"], ["something new"])
        self.assertEqual(snapshot["notes"], "ambiguous page")

    async def test_field_aliases_units_and_invisible_characters_are_normalized(self):
        store = EvidenceStore()
        result = await store.publish("material", {"Materialnummer": "M\u200b-1", "Hub": "100", "Bohrung": "50",
                                                  "Lieferzeit Produktion": "6 Wochen"}, ERP_AGENT)
        self.assertEqual(result.fact.key, "material:M-1")
        value = result.fact.value
        self.assertEqual((value["stroke"], value["bore"]), ("100", "50"))
        self.assertEqual((value["production_lead_time_weeks"], value["production_lead_time_weeks_text"]),
                         (6, "6 Wochen"))
        self.assertIn("missing status", result.fact.issues)

    async def test_context_for_gives_only_the_named_facts(self):
        store = EvidenceStore()
        await store.publish_facts({"tender": {"reference": "REF-X"},
                                   "positions": [{"number": "1"}, {"number": "2"}]}, TENDER_AGENT)
        self.assertEqual(list(store.context_for(["position:2", "position:9"])), ["position:2"])

    async def test_concurrent_publishes_of_one_key_version_atomically(self):
        store = EvidenceStore()
        await asyncio.gather(*(store.publish("position", {"number": "1", "quantity": str(n)}, TENDER_AGENT)
                               for n in range(20)))
        versions = [f.version for f in store.history("position:1")]
        self.assertEqual(versions, list(range(1, len(versions) + 1)))
        self.assertEqual(sum(f.status == FactStatus.VALIDATED for f in store.history("position:1")), 1)


class TheScheduler(unittest.IsolatedAsyncioTestCase):
    async def test_a_satisfied_dependency_plans_one_task_per_parameter_set_once(self):
        h = Harness()
        self.assertEqual(sorted(t.spec for t in h.scheduler.evaluate()),
                         ["discover_tender", "read_specification", "search_mails"])
        self.assertEqual(h.scheduler.evaluate(), [])                    # nothing new: no duplicates
        await h.store.publish_facts({"requirements": [{"id": "R-1"}, {"id": "R-2"}]}, SPEC_AGENT)
        new = h.scheduler.evaluate()
        self.assertEqual([(t.spec, t.params) for t in new],
                         [("find_materials", {"requirement_id": "R-1"}), ("find_materials", {"requirement_id": "R-2"})])
        self.assertEqual(new[0].context_keys, ["requirement:R-1"])
        self.assertEqual(new[0].version_seen, {"requirement:R-1": 1})
        self.assertEqual(h.scheduler.evaluate(), [])                    # two events later: still no duplicates
        self.assertEqual(len(h.logged("dependency_satisfied")), 1)

    async def test_a_changed_input_makes_a_done_task_stale_and_replans_it(self):
        h = Harness()
        await h.store.publish_facts({"requirements": [{"id": "R-1", "bore": "a"}]}, SPEC_AGENT)
        [task] = [t for t in h.scheduler.evaluate() if t.spec == "find_materials"]
        h.scheduler.mark_running(task.id)
        h.scheduler.mark_done(task.id)
        self.assertEqual(h.scheduler.evaluate(), [])
        await h.store.publish_facts({"requirements": [{"id": "R-1", "bore": "b"}]}, SPEC_AGENT)
        stale = h.scheduler.on_fact_changed("requirement:R-1", 2)
        self.assertEqual([t.id for t in stale], [task.id])
        [again] = h.scheduler.evaluate()
        self.assertEqual((again.spec, again.params, again.attempts), ("find_materials", {"requirement_id": "R-1"}, 2))
        self.assertEqual(again.version_seen, {"requirement:R-1": 2})
        self.assertEqual(h.logged("task_rescheduled")[0]["reason"], "stale input")

    async def test_the_default_graph_batches_requirements_per_wave_and_rebatches_only_what_changed(self):
        h = Harness(graph=default_graph())
        h.scheduler.evaluate()
        await h.store.publish_facts({"requirements": [{"id": "A-1", "kataloggruppe": "g"},
                                                      {"id": "A-2", "kataloggruppe": "g"}]}, SPEC_AGENT)
        [first] = h.scheduler.evaluate()
        self.assertEqual((first.spec, first.params), ("find_materials", {"requirement_ids": ["A-1", "A-2"]}))
        self.assertEqual(h.scheduler.evaluate(), [])
        await h.store.publish_facts({"requirements": [{"id": "A-3", "kataloggruppe": "g"}]}, SPEC_AGENT)
        [second] = h.scheduler.evaluate()                            # a later wave: only the new requirement
        self.assertEqual(second.params, {"requirement_ids": ["A-3"]})
        for task in (first, second):
            h.scheduler.mark_running(task.id)
            h.scheduler.mark_done(task.id)
        await h.store.publish_facts({"requirements": [{"id": "A-2", "kataloggruppe": "g", "bore": "new"}]}, SPEC_AGENT)
        h.scheduler.on_fact_changed("requirement:A-2", 2)
        [again] = h.scheduler.evaluate()                             # only the changed requirement is redone
        self.assertEqual(again.params, {"requirement_ids": ["A-2"]})
        self.assertIn("requirement:A-2", again.context_keys)

    async def test_a_failed_task_is_retried_once(self):
        h = Harness()
        [task] = [t for t in h.scheduler.evaluate() if t.spec == "discover_tender"]
        h.scheduler.mark_failed(task.id, "boom")
        [retry] = h.scheduler.evaluate()
        self.assertEqual((retry.dedup_key, retry.attempts), (dedup_key("discover_tender", {}), 2))
        h.scheduler.mark_failed(retry.id, "boom again")
        self.assertEqual(h.scheduler.evaluate(), [])


class TheOrchestrator(unittest.IsolatedAsyncioTestCase):
    async def test_four_workers_run_in_parallel(self):
        h = Harness()
        first_start: dict[str, float] = {}

        def slow(name):
            async def handle(task, context):
                first_start.setdefault(name, time.monotonic())
                await asyncio.sleep(0.2)
                return {"tender": {"reference": "REF-X"}} if name == "tender" else None
            return handle

        for name in ("tender", "spec", "erp", "mail"):
            h.worker(name, slow(name))
        await h.store.publish_facts({"requirements": [{"id": "R-1"}]}, SPEC_AGENT)   # so the ERP has a task too
        started = time.monotonic()
        await h.run()
        self.assertLess(time.monotonic() - started, 0.8)   # four 0.2s tasks + a 0.2s follow-up, not serial
        self.assertEqual(len(first_start), 4)
        self.assertLess(max(first_start.values()) - min(first_start.values()), 0.1)

    async def test_evidence_raises_an_event_that_assigns_a_task_to_the_right_worker(self):
        h = Harness()
        tender = h.worker("tender", lambda t, c: {"tender": {"reference": "REF-X"}, "positions": [{"number": "1"}]})
        h.worker("spec", nothing)
        h.worker("erp", nothing)
        mail = h.worker("mail", lambda t, c: {"approvals": [{"reference": t.params["reference"], "percent": 1}]}
                        if t.spec == "check_discount_approval" else None)
        facts = await h.run()
        self.assertEqual([t.spec for t in tender.seen], ["discover_tender"])
        self.assertEqual([t.spec for t in mail.seen], ["search_mails", "check_discount_approval"])
        followup = mail.seen[1]
        self.assertEqual(followup.params, {"reference": "REF-X"})
        self.assertEqual(followup.context, {"tender:REF-X": {"reference": "REF-X"}})   # only what it depends on
        types = [e.type for e in h.bus.history]
        self.assertLess(types.index(EventType.TENDER_DISCOVERED), types.index(EventType.DISCOUNT_APPROVAL_FOUND))
        self.assertEqual(facts["mail"]["approvals"], [{"reference": "REF-X", "percent": 1}])
        self.assertEqual(h.scheduler.summary(), {"done": 4})   # three roots and the follow-up; no requirement, no ERP
        for name in ("task_created", "task_started", "task_finished", "evidence_published", "evidence_validated",
                     "dependency_satisfied"):
            self.assertTrue(h.logged(name), name)

    async def test_a_task_is_never_planned_twice_at_the_same_time(self):
        h = Harness()
        # the tender worker reports the same tender twice and the spec worker reports requirements twice:
        # neither produces a second check_discount_approval or find_materials task
        h.worker("tender", lambda t, c: {"tender": {"reference": "REF-X"}, "positions": [{"number": "1"}]})
        h.worker("spec", lambda t, c: {"requirements": [{"id": "R-1"}]})
        erp = h.worker("erp", nothing, delay=0.1)
        mail = h.worker("mail", nothing, delay=0.1)
        for _ in range(3):
            await h.store.publish_facts({"tender": {"reference": "REF-X"}}, TENDER_AGENT)
            await h.store.publish_facts({"requirements": [{"id": "R-1"}]}, SPEC_AGENT)
        await h.run()
        self.assertEqual([t.spec for t in mail.seen], ["search_mails", "check_discount_approval"])
        self.assertEqual([t.spec for t in erp.seen], ["find_materials"])
        keys = [t.dedup_key for t in h.scheduler.tasks.values()]
        self.assertEqual(len(keys), len(set(keys)))

    async def test_an_updated_fact_invalidates_dependent_results_and_reruns_them(self):
        h = Harness()
        versions_seen = []

        def erp_handle(task, context):
            versions_seen.append(task.version_seen["requirement:R-1"])
            return {"materials": [{"material_number": f"M-{len(versions_seen)}", "found_for": "R-1"}]}

        h.worker("tender", nothing)
        h.worker("spec", lambda t, c: {"requirements": [{"id": "R-1", "bore": "a"}]})
        h.worker("erp", erp_handle)
        h.worker("mail", nothing, delay=0.5)   # still busy when the requirement changes
        orchestrator_run = asyncio.create_task(h.run())
        while not any(t.status == TaskStatus.DONE and t.spec == "find_materials" for t in h.scheduler.tasks.values()):
            await asyncio.sleep(0.01)
        await h.store.publish_facts({"requirements": [{"id": "R-1", "bore": "b"}]}, SPEC_AGENT)  # a revision
        await orchestrator_run
        self.assertEqual(versions_seen, [1, 2])
        self.assertIn(EventType.EVIDENCE_INVALIDATED, [e.type for e in h.bus.history])
        self.assertTrue(h.logged("task_stale") and h.logged("task_rescheduled") and h.logged("evidence_updated"))
        done = [t for t in h.scheduler.tasks.values() if t.spec == "find_materials"]
        self.assertEqual([(t.status, t.stale) for t in done], [(TaskStatus.DONE, True), (TaskStatus.DONE, False)])

    async def test_invalid_evidence_triggers_no_downstream_task(self):
        h = Harness()
        h.worker("tender", lambda t, c: {"tender": {"title": "no reference"}})   # fails the schema
        h.worker("spec", nothing)
        h.worker("erp", nothing)
        mail = h.worker("mail", nothing)
        await h.run()
        self.assertEqual([t.spec for t in mail.seen], ["search_mails"])
        self.assertEqual(h.store.values("tender"), [])
        self.assertTrue(h.logged("evidence_rejected"))

    async def test_a_failing_worker_does_not_block_the_others(self):
        h = Harness()

        def explode(task, context):
            raise RuntimeError("browser gone")

        h.worker("tender", explode)
        spec = h.worker("spec", lambda t, c: {"requirements": [{"id": "R-1"}]})
        erp = h.worker("erp", lambda t, c: {"materials": [{"material_number": "M-1"}]})
        mail = h.worker("mail", nothing)
        facts = await h.run()
        self.assertEqual(len(spec.seen), 1)
        self.assertEqual([t.spec for t in erp.seen], ["find_materials"])
        self.assertEqual(len(mail.seen), 1)
        failed = [t for t in h.scheduler.tasks.values() if t.status == TaskStatus.FAILED]
        self.assertEqual([t.attempts for t in failed], [1, 2])    # tried, retried once, then left alone
        self.assertIn("browser gone", failed[0].error)
        self.assertEqual(facts["erp"]["materials"], [{"material_number": "M-1"}])
        self.assertEqual(len(h.logged("task_failed")), 2)

    async def test_interim_progress_starts_dependent_tasks_before_the_source_task_ends(self):
        h = Harness()
        spec_done = asyncio.Event()

        async def spec_handle(task, context):
            spec.report_progress({"requirements": [{"id": "R-1"}]})   # saved early, like save_facts
            await spec_done.wait()                                    # the specification goes on for a while
            return {"requirements": [{"id": "R-1"}, {"id": "R-2"}]}

        spec_running_at_first_erp_task = []

        async def erp_handle(task, context):
            spec_running_at_first_erp_task.append(spec.current is not None)
            spec_done.set()
            return {"materials": [{"material_number": "M-1"}]}

        h.worker("tender", nothing)
        spec = h.worker("spec", spec_handle)
        erp = h.worker("erp", erp_handle)
        h.worker("mail", nothing)
        await h.run()
        self.assertEqual([t.params for t in erp.seen], [{"requirement_id": "R-1"}, {"requirement_id": "R-2"}])
        self.assertTrue(spec_running_at_first_erp_task[0])   # the ERP started while the spec task was still open
        self.assertTrue(h.logged("progress_published"))
        self.assertEqual(h.scheduler.summary(), {"done": 5})

    async def test_a_stale_task_is_not_rerun_while_its_source_is_still_reporting(self):
        h = Harness()
        spec_may_finish = asyncio.Event()

        async def spec_handle(task, context):
            spec.report_progress({"requirements": [{"id": "R-1", "bore": "a"}]})
            await asyncio.sleep(0.05)
            spec.report_progress({"requirements": [{"id": "R-1", "bore": "b"}]})   # changed while still running
            await spec_may_finish.wait()
            return {"requirements": [{"id": "R-1", "bore": "b"}]}

        erp_runs = []

        async def erp_handle(task, context):
            erp_runs.append((task.version_seen["requirement:R-1"], spec.current is not None))
            await asyncio.sleep(0.1)
            spec_may_finish.set()
            return {"materials": [{"material_number": "M-1"}]}

        h.worker("tender", nothing)
        spec = h.worker("spec", spec_handle)
        h.worker("erp", erp_handle)
        h.worker("mail", nothing)
        await h.run()
        # first run on v1 while the spec is open; the re-run on v2 only after the spec task has finished
        self.assertEqual(erp_runs, [(1, True), (2, False)])

    async def test_results_arriving_at_the_deadline_are_still_collected(self):
        h = Harness(deadline=0.3)

        async def until_deadline(task, context):
            await asyncio.sleep(0.3)   # finishes just as the orchestrator's clock runs out
            return {"tender": {"reference": "REF-LATE"}}

        h.worker("tender", until_deadline)
        for name in ("spec", "erp", "mail"):
            h.worker(name, nothing)
        facts = await h.run()
        self.assertEqual(facts["tender"]["tender"], {"reference": "REF-LATE"})
        self.assertEqual(h.logged("run_finished")[0]["reason"], "deadline")

    def test_a_first_visit_gets_its_share_of_the_research_window(self):
        from orchestration.agent_worker import SiteWorker
        from orchestration.schemas import Task
        worker = SiteWorker("spec", "http://127.0.0.1:1", Path("."), "", lambda t: "", deadline=0.0)
        worker.window_start, worker.deadline = 1000.0, 1400.0
        first = Task(spec="read_specification", agent="spec", dedup_key="x", time_share=0.5)
        followup = Task(spec="read_price_conditions", agent="erp", dedup_key="y")
        self.assertEqual(worker.task_deadline(first), 1200.0)
        self.assertEqual(worker.task_deadline(followup), 1400.0)

    async def test_the_deadline_ends_the_run(self):
        h = Harness(deadline=0.3)

        async def forever(task, context):
            await asyncio.sleep(10)

        for name in ("tender", "spec", "erp", "mail"):
            h.worker(name, forever)
        started = time.monotonic()
        await h.run()
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(h.logged("run_finished")[0]["reason"], "deadline")


class TheDemo(unittest.IsolatedAsyncioTestCase):
    async def test_the_demo_runs_end_to_end_with_the_default_graph(self):
        with redirect_stdout(io.StringIO()) as out:
            facts = await demo.main()
        text = out.getvalue()
        self.assertIn("TenderDiscovered", text)
        self.assertIn("check_discount_approval", text)
        self.assertEqual(sorted(facts), ["erp", "mail", "spec", "tender"])
        self.assertEqual(len(facts["erp"]["materials"]), 2)   # both requirements, one ERP batch
        self.assertEqual(len(facts["mail"]["approvals"]), 1)
        self.assertIn("dependency_satisfied spec=compute_bid", text)

    def test_the_default_graph_names_only_kinds_of_information(self):
        text = default_graph().describe()
        for forbidden in ("http", ".run", "kyora", "atira"):
            self.assertNotIn(forbidden, text)
        self.assertIn("find_materials [erp]: needs a requirement with a catalogue group", text)

    def test_split_facts_covers_every_prompt_key(self):
        facts = {"tender": {"reference": "r"}, "positions": [{"number": "1"}], "clarifications": [], "documents": [],
                 "revisions": [{"id": "x"}], "current_revision": "x", "requirements": [{"id": "a"}],
                 "materials": [{"material_number": "m"}], "mails": [{"sender": "s"}], "mailbox_domain": "d",
                 "notes": "n", "unexpected": {"a": 1}}
        kinds = [r.kind for r in validation.split_facts(facts)]
        self.assertEqual(kinds, ["tender", "position", "revision", "current_revision", "requirement", "material",
                                 "mail", "mailbox", "note", "unknown"])

    def test_wrapped_and_text_facts_are_unwrapped(self):
        wrapped = {"facts": {"facts": {"materials": [{"material_number": "m"}]}}}
        self.assertEqual([r.kind for r in validation.split_facts(wrapped)], ["material"])
        text = json.dumps({"mails": [{"sender": "s"}]})
        self.assertEqual([r.kind for r in validation.split_facts(text)], ["mail"])
        self.assertEqual(validation.as_facts("not json"), {"notes": "not json"})
        self.assertIsNone(validation.as_facts(None))
        self.assertIsNone(validation.as_facts({"facts": {}}))

    async def test_a_worker_reporting_text_instead_of_json_does_not_fail(self):
        h = Harness(graph=small_graph())
        h.worker("tender", lambda t, c: json.dumps({"tender": {"reference": "REF-TXT"}}))
        for name in ("spec", "erp", "mail"):
            h.worker(name, lambda t, c: None)
        facts = await h.run()
        self.assertEqual(facts["tender"]["tender"], {"reference": "REF-TXT"})
        self.assertEqual(h.logged("task_failed"), [])


if __name__ == "__main__":
    unittest.main()
