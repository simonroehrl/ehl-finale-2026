"""The shared evidence store: every fact the agents found, validated, versioned and attributed.

Workers never write here directly: the orchestrator calls `publish` with what a worker reported, the store
validates it (validation.py), compares it with what it already holds and raises an event only when something
changed. Reads are plain dict lookups; writes are serialized by one asyncio.Lock, so concurrent publishes can't
interleave a version bump.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable

from . import validation
from .events import EVENT_FOR_KIND, Event, EventBus, EventLog, EventType
from .schemas import Fact, FactStatus, Provenance


@dataclass
class PublishResult:
    fact: Fact
    event: Event | None          # None when nothing changed or the record was not accepted
    changed: bool

    @property
    def accepted(self) -> bool:
        return self.fact.status == FactStatus.VALIDATED


class EvidenceStore:
    def __init__(self, bus: EventBus | None = None, log: EventLog | None = None,
                 normalizer: validation.Normalizer | None = None):
        self.bus = bus or EventBus()
        self.log = log
        self.normalizer = normalizer or validation.RuleNormalizer()
        self._facts: dict[str, Fact] = {}             # current fact per key
        self._history: dict[str, list[Fact]] = {}     # every version per key, oldest first
        self._rejected: list[Fact] = []               # records that failed validation, kept for the record
        self._lock = asyncio.Lock()

    # --- writing -------------------------------------------------------------------------------------------------

    async def publish(self, kind: str, raw: Any, provenance: Provenance, source_key: str | None = None,
                      singleton: bool | None = None) -> PublishResult:
        """Validate one record and store it. Returns the stored fact and the event raised, if any."""
        source_key = source_key or validation.SOURCE_KEY_BY_KIND.get(kind, kind)
        if singleton is None:
            singleton = validation.KIND_BY_SOURCE_KEY.get(source_key, (kind, True))[1]
        async with self._lock:
            probe = validation.validate(kind, raw, provenance, None, self.normalizer)
            existing = self._facts.get(probe.key) if probe.key else None
            outcome = validation.validate(kind, raw, provenance, existing, self.normalizer) if existing else probe
            if outcome.status == FactStatus.PROPOSED:
                fact = Fact(key=outcome.key or f"{kind}:rejected:{len(self._rejected)}", kind=kind,
                            value=outcome.value, status=FactStatus.PROPOSED, provenance=provenance, raw=raw,
                            source_key=source_key, singleton=singleton, issues=outcome.issues)
                self._rejected.append(fact)
                self._log("evidence_rejected", key=fact.key, kind=kind, agent=provenance.agent,
                          issues=outcome.issues)
                return PublishResult(fact, None, False)
            if outcome.status == FactStatus.CONFLICTING:
                fact = Fact(key=f"{outcome.key}#conflict:{provenance.agent}", kind=kind, value=outcome.value,
                            status=FactStatus.CONFLICTING, provenance=provenance, raw=raw, source_key=source_key,
                            singleton=singleton, issues=outcome.issues, conflicts_with=outcome.conflicts_with)
                changed = not (fact.key in self._facts and validation.same_value(self._facts[fact.key].value,
                                                                                  fact.value))
                self._facts[fact.key] = fact
                self._history.setdefault(fact.key, []).append(fact)
                self._log("evidence_conflicting", key=outcome.key, kind=kind, agent=provenance.agent,
                          conflicts_with=outcome.conflicts_with)
                return PublishResult(fact, None, changed)
            if existing is not None and validation.same_value(existing.value, outcome.value):
                # the same fact again: no new version, no event
                if outcome.issues != existing.issues:
                    existing.issues = outcome.issues
                return PublishResult(existing, None, False)
            version = existing.version + 1 if existing else 1
            fact = Fact(key=outcome.key, kind=kind, value=outcome.value, version=version,
                        status=FactStatus.VALIDATED, provenance=provenance, raw=raw, source_key=source_key,
                        singleton=singleton, issues=outcome.issues)
            events: list[Event] = []
            if existing is not None:
                existing.status = FactStatus.INVALIDATED
                events.append(Event(type=EventType.EVIDENCE_INVALIDATED, fact_key=fact.key, kind=kind,
                                    version=existing.version, task_id=provenance.task_id,
                                    detail={"superseded_by": version}))
                self._log("evidence_updated", key=fact.key, kind=kind, version=version, agent=provenance.agent)
            else:
                self._log("evidence_validated", key=fact.key, kind=kind, agent=provenance.agent,
                          issues=outcome.issues or None)
            self._facts[fact.key] = fact
            self._history.setdefault(fact.key, []).append(fact)
            event = Event(type=self._event_type(fact), fact_key=fact.key, kind=kind, version=version,
                          task_id=provenance.task_id, detail={"agent": provenance.agent, "update": existing is not None})
            events.append(event)
            for e in events:
                self.bus.publish(e)
            return PublishResult(fact, event, True)

    async def publish_facts(self, facts: dict[str, Any], provenance: Provenance) -> list[PublishResult]:
        """Everything a worker saved, record by record."""
        self._log("evidence_published", agent=provenance.agent, task=provenance.task_id,
                  keys=sorted(facts) if isinstance(facts, dict) else type(facts).__name__)
        results = []
        for record in validation.split_facts(facts):
            results.append(await self.publish(record.kind, record.raw, provenance, record.source_key,
                                              record.singleton))
        return results

    async def invalidate(self, key: str, reason: str, by: str = "system") -> Fact | None:
        """Withdraw a fact (e.g. a source turned out to be superseded). Raises EvidenceInvalidated."""
        async with self._lock:
            fact = self._facts.get(key)
            if fact is None or fact.status == FactStatus.INVALIDATED:
                return None
            fact.status = FactStatus.INVALIDATED
            fact.issues.append(f"invalidated: {reason}")
            self._log("evidence_invalidated", key=key, kind=fact.kind, reason=reason, by=by)
            self.bus.publish(Event(type=EventType.EVIDENCE_INVALIDATED, fact_key=key, kind=fact.kind,
                                   version=fact.version, detail={"reason": reason}))
            return fact

    # --- reading -------------------------------------------------------------------------------------------------

    def get(self, key: str) -> Fact | None:
        return self._facts.get(key)

    def read(self, kind: str | None = None, keys: Iterable[str] | None = None,
             status: FactStatus | None = FactStatus.VALIDATED) -> list[Fact]:
        """Facts by kind and/or keys, validated ones unless `status` says otherwise (None: every status)."""
        facts = [self._facts[k] for k in keys if k in self._facts] if keys is not None else list(self._facts.values())
        if kind is not None:
            facts = [f for f in facts if f.kind == kind]
        if status is not None:
            facts = [f for f in facts if f.status == status]
        return facts

    def values(self, kind: str) -> list[dict[str, Any]]:
        return [f.value for f in self.read(kind=kind)]

    def history(self, key: str) -> list[Fact]:
        return list(self._history.get(key, []))

    def context_for(self, keys: Iterable[str]) -> dict[str, Any]:
        """Only the named facts, as a worker should see them: key -> value (validated or conflicting, marked)."""
        out: dict[str, Any] = {}
        for key in keys:
            fact = self._facts.get(key)
            if fact is None or fact.status in (FactStatus.INVALIDATED, FactStatus.PROPOSED):
                continue
            value = dict(fact.value)
            if fact.status == FactStatus.CONFLICTING:
                value["_conflict"] = fact.issues
            out[key] = value
        return out

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """The facts in the shape the site agents saved them and the analyst reads them: per agent, per source
        key; lists for list kinds, one record for singletons. Conflicting records are included and marked, rejected
        ones listed under `_rejected` so nothing is lost silently."""
        out: dict[str, dict[str, Any]] = {}
        for fact in self._facts.values():
            if fact.status == FactStatus.INVALIDATED:
                continue
            bucket = out.setdefault(fact.provenance.agent, {})
            value = dict(fact.value)
            if fact.status == FactStatus.CONFLICTING:
                value["_conflict"] = fact.issues
            if fact.kind == "unknown":
                bucket.setdefault(fact.value.get("source_key", "unknown"), fact.value.get("payload"))
            elif fact.kind == "note":
                bucket["notes"] = value.get("text")
            elif fact.kind == "mailbox":
                bucket["mailbox_domain"] = value.get("domain")
            elif fact.kind == "current_revision":
                bucket["current_revision"] = value.get("id")
            elif fact.singleton:
                bucket[fact.source_key] = value
            else:
                bucket.setdefault(fact.source_key, []).append(value)
        for fact in self._rejected:
            out.setdefault(fact.provenance.agent, {}).setdefault("_rejected", []).append(
                {"source_key": fact.source_key, "issues": fact.issues, "raw": fact.raw})
        return out

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for fact in self._facts.values():
            if fact.status == FactStatus.VALIDATED:
                counts[fact.kind] = counts.get(fact.kind, 0) + 1
        return counts

    # --- internals -----------------------------------------------------------------------------------------------

    @staticmethod
    def _event_type(fact: Fact) -> EventType:
        if fact.kind == "material" and fact.value.get("price_conditions"):
            return EventType.PRICING_DATA_UPDATED
        return EVENT_FOR_KIND.get(fact.kind, EventType.EVIDENCE_UPDATED)

    def _log(self, name: str, **fields):
        if self.log:
            self.log.write(name, **{k: v for k, v in fields.items() if v is not None})
