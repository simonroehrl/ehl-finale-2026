"""Validation and normalization: a worker's raw record in, a canonical record with its key and status out.

Deterministic rules only: field aliases, light unit handling, identity derivation, conflict detection. Values stay
as the websites show them (dates with format hints, numbers with locale hints); the analyst interprets them later.
`Normalizer` is the seam for a semantic (model-based) normalizer: implement it and pass it to the store.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from . import schemas
from .schemas import Fact, FactStatus, Provenance

# Which top-level key of a worker's facts holds which kind of record, and whether it is one record or a list.
KIND_BY_SOURCE_KEY: dict[str, tuple[str, bool]] = {   # source key -> (kind, singleton)
    "tender": ("tender", True),
    "positions": ("position", False),
    "clarifications": ("clarification", False),
    "documents": ("document", False),
    "document": ("document", True),
    "revisions": ("revision", False),
    "current_revision": ("current_revision", True),
    "requirements": ("requirement", False),
    "materials": ("material", False),
    "price_conditions": ("price_condition", False),
    "mails": ("mail", False),
    "approvals": ("approval", False),
    "mailbox_domain": ("mailbox", True),
    "notes": ("note", True),
}
SOURCE_KEY_BY_KIND: dict[str, str] = {kind: key for key, (kind, _) in KIND_BY_SOURCE_KEY.items()}
SOURCE_KEY_BY_KIND["document"] = "documents"

MODELS: dict[str, type[schemas.Evidence]] = {
    "tender": schemas.Tender, "position": schemas.TenderPosition, "clarification": schemas.Clarification,
    "document": schemas.Document, "revision": schemas.SpecRevision, "current_revision": schemas.CurrentRevision,
    "requirement": schemas.Requirement, "material": schemas.Material, "price_condition": schemas.PriceCondition,
    "mail": schemas.Mail, "approval": schemas.Approval, "mailbox": schemas.Mailbox, "note": schemas.Note,
    "unknown": schemas.UnknownInfo,
}

# The attribute that identifies a record of each kind, and the second one for records without a natural id.
IDENTITY: dict[str, list[str]] = {
    "tender": ["reference"], "position": ["number"], "clarification": ["number"], "document": ["name"],
    "revision": ["id"], "requirement": ["id"], "material": ["material_number"],
    "price_condition": ["material_number", "valid_from"], "approval": ["reference", "date"],
    "mail": ["id"],
}

# Attributes worth flagging when a validated record lacks them (the record stays valid).
EXPECTED: dict[str, list[str]] = {
    "position": ["quantity", "requirement"],
    "requirement": ["catalogue_group", "bore", "stroke", "pressure", "mounting", "certification"],
    "material": ["status", "bore", "stroke", "pressure", "mounting", "certification"],
    "mail": ["date", "subject", "body"],
    "clarification": ["date", "text"],
    "revision": ["date", "status"],
}

_TECH = {"bore": ["bohrung", "kolbendurchmesser", "bore_mm", "kolben"],
         "stroke": ["hub", "hub_mm", "stroke_mm"],
         "pressure": ["druck", "pressure_bar", "betriebsdruck", "nenndruck", "druckstufe"],
         "mounting": ["befestigung", "montage", "anbau", "mounting_type", "befestigungsart", "anbauart"],
         "certification": ["zertifizierung", "zertifikat", "cert", "certificate", "zulassung"]}

ALIASES: dict[str, dict[str, list[str]]] = {   # kind -> canonical -> aliases (already lower_snake)
    "tender": {"reference": ["ref", "referenz", "tender_reference", "ausschreibungsnummer", "vergabenummer",
                             "aktenzeichen", "reference_number", "id", "number"],
               "title": ["titel", "name", "bezeichnung"], "buyer": ["auftraggeber", "vergabestelle", "kaeufer"],
               "deadline": ["frist", "angebotsfrist", "abgabefrist", "submission_deadline", "due"]},
    "position": {"number": ["position", "pos", "position_number", "nr", "no", "positionsnummer", "id", "pos_nr"],
                 "description": ["bezeichnung", "beschreibung", "text", "title", "kurztext"],
                 "quantity": ["menge", "qty", "amount"], "unit": ["einheit", "me", "mengeneinheit"],
                 "requirement": ["requirement_id", "anforderung", "requirement_ref", "refers_to", "spec_ref",
                                 "requirement_reference"],
                 "tender_reference": ["reference", "tender", "referenz"]},
    "clarification": {"number": ["id", "nr", "no", "answer_number", "nummer", "antwort_nr", "frage_nr"],
                      "date": ["datum", "date_time", "datetime", "timestamp", "zeitpunkt"],
                      "text": ["answer", "answer_text", "antwort", "body", "content", "full_text"],
                      "changes": ["change", "effect", "aenderung", "änderung", "summary", "what_changes", "impact"],
                      "withdraws": ["withdrawn", "withdraws_answer", "zieht_zurueck", "zurueckgezogen",
                                    "withdraws_number"]},
    "document": {"name": ["title", "titel", "filename", "file", "dateiname", "dokumentnummer", "document_number",
                          "number", "id", "nummer", "bezeichnung"],
                 "content": ["summary", "description", "contains", "inhalt", "beschreibung"]},
    "revision": {"id": ["revision", "rev", "revision_id", "number", "version", "name", "nr"],
                 "date": ["datum", "revision_date"], "status": ["state", "zustand"],
                 "change_note": ["note", "aenderung", "änderung", "changes", "change", "aenderungsvermerk"],
                 "marking": ["marker", "label", "kennzeichnung", "markierung", "shown_as"]},
    "current_revision": {"id": ["revision", "current", "value", "name"]},
    "requirement": {"id": ["requirement", "requirement_id", "number", "nr", "kennung", "name", "anforderung"],
                    "revision": ["revision_id", "rev"],
                    "catalogue_group": ["kataloggruppe", "catalog_group", "katalog_gruppe", "catalogue",
                                        "catalog", "group", "gruppe"],
                    "pieces_per_set": ["stueck_pro_satz", "stück_pro_satz", "pcs_per_set", "set_size",
                                       "pieces_per_satz", "satz"],
                    "wording": ["original_wording", "originalwortlaut", "full_text", "wortlaut"], **_TECH},
    "material": {"material_number": ["material", "materialnummer", "material_no", "number", "nr", "id",
                                     "artikelnummer", "sku", "matnr", "material_id"],
                 "description": ["bezeichnung", "beschreibung", "text", "name"], "status": ["state", "zustand"],
                 "found_for": ["requirement", "requirement_id", "for_requirement", "found_for_requirement"],
                 "matches_requirement": ["matches", "match", "exact_match"],
                 "price_conditions": ["prices", "conditions", "preiskonditionen", "konditionen",
                                      "price_condition", "pricing"],
                 "stock": ["bestand", "lagerbestand", "stock_pieces", "on_stock", "inventory"],
                 "stock_lead_time_weeks": ["stock_lead_time", "lieferzeit_lager", "lead_time_stock_weeks",
                                           "lead_time_from_stock_weeks", "lead_time_from_stock"],
                 "production_lead_time_weeks": ["production_lead_time", "lieferzeit_produktion",
                                                "lead_time_production_weeks", "lead_time_production"],
                 "pieces_per_carton": ["carton_size", "karton", "stueck_pro_karton", "vpe", "pcs_per_carton"],
                 **_TECH},
    "price_condition": {"material_number": ["material", "materialnummer", "material_no"],
                        "valid_from": ["from", "gueltig_ab", "gültig_ab", "validity_from", "start"],
                        "valid_to": ["to", "gueltig_bis", "gültig_bis", "validity_to", "end"],
                        "currency": ["waehrung", "währung", "cur"], "price_unit": ["preiseinheit", "unit", "per"],
                        "scales": ["staffeln", "staffel", "quantity_scales", "tiers", "prices"],
                        "fx_key": ["fx", "devisenschluessel", "fx_code", "currency_key"]},
    "mail": {"sender": ["from", "absender", "sender_address", "from_address", "von"],
             "date": ["datum", "sent", "received", "datetime", "date_time", "time"],
             "subject": ["betreff", "title"], "body": ["text", "content", "body_text", "message", "inhalt"],
             "folder": ["ordner", "mailbox", "location"], "reference": ["tender_reference", "referenz", "ref"],
             "id": ["mail_id", "message_id", "uid"]},
    "approval": {"percent": ["discount", "discount_percent", "rabatt", "prozent", "rabatt_prozent"],
                 "reference": ["tender_reference", "referenz", "ref"], "sender": ["from", "absender"],
                 "date": ["datum", "datetime"],
                 "min_order_value": ["threshold", "mindestbestellwert", "minimum_order_value", "min_value"]},
    "mailbox": {"domain": ["mailbox_domain", "value"]},
    "note": {"text": ["notes", "note", "value"]},
}

_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]")
_ID_LIKE = re.compile(r"(^|_)(id|nr|no|number|num|code|key|ref|reference|label|kennung|nummer)($|_)")
_WEEKS = re.compile(r"^\s*(\d+)\s*(wochen|woche|weeks|week|wo|w)\.?\s*$", re.I)


class Normalizer(Protocol):
    def normalize(self, kind: str, raw: dict[str, Any]) -> dict[str, Any]: ...


class RuleNormalizer:
    """Field names to their canonical spelling, nested `attributes` lifted, lead times in weeks as integers where
    the text is unambiguous. Nothing else is converted."""

    def normalize(self, kind: str, raw: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in raw.items():
            out[_snake(key)] = value
        nested = out.get("attributes")
        if isinstance(nested, dict):
            for key, value in nested.items():
                out.setdefault(_snake(key), value)
        for canonical, aliases in ALIASES.get(kind, {}).items():
            if canonical in out:
                continue
            for alias in aliases:
                if alias in out:
                    out[canonical] = out.pop(alias)
                    break
        for key in list(out):
            if key.endswith("_weeks") and isinstance(out[key], str):
                match = _WEEKS.match(out[key])
                if match:
                    out[key + "_text"] = out[key]
                    out[key] = int(match.group(1))
        identity = IDENTITY.get(kind, [None])[0]
        if identity and out.get(identity) in (None, ""):
            # no known spelling of the identifier: take the first identifier-looking scalar attribute that is not
            # another known attribute of this kind (a position's tender_reference is not its number)
            known = set(ALIASES.get(kind, {}))
            for key, value in out.items():
                if key in known or not _ID_LIKE.search(key):
                    continue
                if isinstance(value, (str, int)) and str(value).strip():
                    out[identity] = value
                    out["_identity_from"] = key
                    break
        return out


def _snake(key: Any) -> str:
    text = str(key).strip().lower()
    text = re.sub(r"[\s\-./]+", "_", text)
    return text.strip("_")


def clean_id(value: Any) -> str:
    """An identifier as text, without invisible characters and surplus whitespace."""
    return re.sub(r"\s+", " ", _INVISIBLE.sub("", str(value))).strip()


# --- splitting a worker's facts into records ----------------------------------------------------------------------

@dataclass
class Record:
    kind: str
    source_key: str
    singleton: bool
    raw: Any


WRAPPER_KEYS = {"facts", "data", "result", "results", "payload", "output"}


def as_facts(value: Any) -> dict[str, Any] | None:
    """What a worker saved as a facts dict: JSON text is parsed, a wrapper like `{"facts": {...}}` is unwrapped
    (models nest the tool's own argument name), anything else that is not a dict becomes a note."""
    if value in (None, "", {}, []):
        return None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"notes": value}
        return as_facts(parsed)
    if not isinstance(value, dict):
        return {"notes": value}
    while len(value) == 1 and _snake(next(iter(value))) in WRAPPER_KEYS and isinstance(next(iter(value.values())), dict):
        value = next(iter(value.values()))
    return value or None


def split_facts(facts: Any) -> list[Record]:
    """A worker's saved JSON as individual records: known keys by kind, anything else as `unknown`."""
    records: list[Record] = []
    facts = as_facts(facts)
    if facts is None:
        return records
    for source_key, payload in facts.items():
        kind, singleton = KIND_BY_SOURCE_KEY.get(_snake(source_key), ("unknown", True))
        if kind == "unknown":
            if payload in (None, "", [], {}):
                continue
            records.append(Record("unknown", source_key, True, {"source_key": source_key, "payload": payload}))
        elif singleton:
            if payload in (None, "", [], {}):
                continue
            if kind == "note":
                raw = {"text": payload}        # notes come as text, lists or dicts: all of it is the note
            else:
                raw = payload if isinstance(payload, dict) else {_scalar_field(kind): payload}
            records.append(Record(kind, source_key, True, raw))
        else:
            for item in _items(kind, payload):
                if item in (None, "", {}):
                    continue
                raw = item if isinstance(item, dict) else {IDENTITY.get(kind, ["value"])[0]: item}
                records.append(Record(kind, source_key, False, raw))
    return records


def _items(kind: str, payload: Any) -> list[Any]:
    """A list kind's payload as items. Models also save such lists as a mapping id -> record
    (`{"A-1": {...}, "A-2": {...}}`): then the mapping key is the record's identity."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and payload and all(isinstance(v, dict) for v in payload.values()):
        identity = IDENTITY.get(kind, ["id"])[0]
        return [{identity: key, **value} if not any(_snake(k) == identity for k in value) else value
                for key, value in payload.items()]
    return [payload]


def _scalar_field(kind: str) -> str:
    return {"current_revision": "id", "mailbox": "domain", "note": "text"}.get(kind, "value")


# --- validation ------------------------------------------------------------------------------------------------------

@dataclass
class Outcome:
    status: FactStatus
    value: dict[str, Any]
    key: str | None = None
    issues: list[str] = field(default_factory=list)
    conflicts_with: str | None = None


def validate(kind: str, raw: Any, provenance: Provenance, existing: Fact | None = None,
             normalizer: Normalizer | None = None) -> Outcome:
    """Normalize and check one record against its schema, derive its key, and compare it with the fact already
    stored under that key (if any) to tell an update from a conflict."""
    normalizer = normalizer or RuleNormalizer()
    if not isinstance(raw, dict):
        return Outcome(FactStatus.PROPOSED, {"value": raw}, None, [f"{kind}: a record must be an object"])
    value = normalizer.normalize(kind, raw)
    model = MODELS.get(kind, schemas.UnknownInfo)
    try:
        value = model.model_validate(value).model_dump(exclude_none=True)
    except ValidationError as error:
        issues = [f"{'.'.join(str(p) for p in e['loc']) or kind}: {e['msg']}" for e in error.errors()]
        return Outcome(FactStatus.PROPOSED, value, None, issues)
    for name in IDENTITY.get(kind, [])[:1]:
        if value.get(name) not in (None, ""):
            value[name] = clean_id(value[name])
    issues = [f"missing {name}" for name in EXPECTED.get(kind, []) if value.get(name) in (None, "", [], {})]
    key = key_for(kind, value, provenance)
    if existing is not None and existing.status == FactStatus.VALIDATED and not same_value(existing.value, value):
        if existing.provenance.agent != provenance.agent or existing.provenance.site != provenance.site:
            issues.append(f"contradicts {existing.key} v{existing.version} from {existing.provenance.agent}")
            return Outcome(FactStatus.CONFLICTING, value, key, issues, conflicts_with=existing.key)
    return Outcome(FactStatus.VALIDATED, value, key, issues)


def key_for(kind: str, value: dict[str, Any], provenance: Provenance) -> str:
    parts = [clean_id(value[name]) for name in IDENTITY.get(kind, []) if value.get(name) not in (None, "")]
    if kind == "mail" and not parts:
        digest = hashlib.sha1(json.dumps([value.get("sender"), value.get("date"), value.get("subject")],
                                         ensure_ascii=False, default=str).encode()).hexdigest()[:10]
        parts = [str(value.get("folder") or "inbox"), digest]
    if kind in ("mailbox", "note", "current_revision", "document") and not parts:
        parts = [provenance.agent]
    if kind == "unknown":
        parts = [provenance.agent, clean_id(value.get("source_key", "?"))]
    if not parts:
        parts = [provenance.agent, hashlib.sha1(canonical_json(value).encode()).hexdigest()[:10]]
    return ":".join([kind, *parts])


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def same_value(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return canonical_json(a) == canonical_json(b)
