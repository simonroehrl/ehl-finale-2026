"""Turn a page-sized Qwen reply into Evidence. Only the current task's record kinds are asked for."""
from __future__ import annotations

import re

from evidence import Case, Evidence, Message, QAAnswer, SpecRevision, TenderPosition, Material, ErpSearch

from actions import parse_json
from board import Task

PENDING_DOMAIN = "pending.local"
_ISO = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})"
_SCENARIO = re.compile(rf"scenario date(?: is)?\s*[:=]?\s*({_ISO})", re.I)
_ISO_ANY = re.compile(rf"({_ISO})")
_REF_LABEL = re.compile(
    r"(?:tender reference|vergabenummer|aktenzeichen|ausschreibung)\s*[:=]\s*`?([A-Z0-9][A-Z0-9._/-]{3,})",
    re.I)
_REF_PAREN = re.compile(r"\(([A-Z]{1,8}[-/][A-Z0-9][-A-Z0-9/]{2,})\)")
_DOMAIN_LABEL = re.compile(
    r"(?:mailbox domain|mail domain|bidder'?s mailbox domain)\s*[:=]?\s*`?([a-z0-9.-]+\.[a-z]{2,})",
    re.I)
_EMAIL_DOMAIN = re.compile(r"\b[a-z0-9._%+-]+@([a-z0-9.-]+\.[a-z]{2,})\b", re.I)

SHAPES = {
    "positions": {
        "positions": [{"position": "", "quantity": "", "unit_raw": "", "unit_kind": "piece",
                       "requirement_id": None}],
    },
    "qa": {
        "qa": [{"number": "", "date": "", "reference": None, "text": "", "withdraws": [],
                "quantity_changes": [{"position": "", "quantity": "", "unit_kind": "piece"}],
                "amendments": [{"revision": "", "requirement_id": "",
                                "attributes": {"bore_mm": None, "stroke_mm": None, "pressure_bar": None,
                                               "mounting": None, "certification": None}}],
                "assignments": [{"position": "", "condition_group": None, "packaging_tariff": None,
                                 "delivery_commitment": None, "setup_pool": None, "bonus_plan": None,
                                 "setup_fee_eur": None}]}],
    },
    "revisions": {
        "revisions": [{"id": "", "document": None, "date": None, "marking": "unknown", "pieces_per_set": None,
                       "requirements": [{"id": "", "catalog_group": None, "usable_stroke": False,
                                         "end_reserve_key": None, "pieces_per_set": None, "bore_mm": None,
                                         "stroke_mm": None, "pressure_bar": None, "mounting": None,
                                         "certification": None}]}],
    },
    "erp": {
        "materials": [{"number": "", "status": "other", "status_raw": None, "catalog_group": None,
                       "attributes": {"bore_mm": None, "stroke_mm": None, "pressure_bar": None,
                                      "mounting": None, "certification": None},
                       "stock_pieces": None, "stock_lead_time_weeks": None, "production_lead_time_weeks": None,
                       "pieces_per_carton": None, "allocation_mass_g": None, "surcharge_mass_kg": None,
                       "metal_index": None,
                       "price_conditions": [{"valid_from": "", "valid_to": None, "currency": "EUR",
                                             "fx_key": None, "price_unit": 1,
                                             "scales": [{"min_quantity": 1, "price": ""}]}]}],
        "erp_searches": [{"catalog_group": "", "material_numbers": [], "complete": False}],
    },
    "mail": {
        "messages": [{"id": "", "folder": None, "sender": "", "date": "", "subject": "", "reference": None,
                      "kind": "other", "discount_percent": None, "min_order_value_eur": None,
                      "condition_rules": [{"key": "", "scope": "base", "text": ""}],
                      "metal_index_rates": [{"code": "", "eur_per_kg": "", "valid_from": None, "valid_to": None}],
                      "technical_releases": [{"key": "", "revision": "", "reserve_mm_per_end": ""}],
                      "packaging_tariffs": [{"key": "", "eur_per_carton": ""}],
                      "delivery_commitments": [{"key": "", "material": "", "max_pieces": 0, "lead_time_weeks": 0}],
                      "fx_rates": [{"key": "", "currency": "", "eur_per_unit": ""}],
                      "setup_pools": [{"key": "", "fee_eur": "",
                                       "allocation": "other"}],
                      "bonus_plans": [{"key": "", "allocation": "other",
                                       "bands": [{"lower_eur": "0", "upper_eur": None, "percent": "0"}]}]}],
    },
    "tan": {"tan": None, "id": None, "subject": None},
}


def kind_for(task: Task) -> str:
    q = task.question.casefold()
    if task.site == "tender-portal":
        if any(word in q for word in ("q&a", "qa", "answer", "frage", "klär")):
            return "qa"
        return "positions"
    if task.site == "customer-portal":
        return "revisions"
    if task.site == "erp":
        return "erp"
    if task.site == "mail":
        return "mail"
    return "positions"


def infer_unit(raw: str | None) -> str:
    text = (raw or "").casefold()
    if "satz" in text or text in {"set", "sets"}:
        return "set"
    return "piece"


def _clean_unit(row: dict) -> dict:
    if not row.get("unit_kind") and row.get("unit_raw"):
        row["unit_kind"] = infer_unit(str(row["unit_raw"]))
    if row.get("unit_kind") not in {"piece", "set", "other"}:
        row["unit_kind"] = infer_unit(str(row.get("unit_raw") or ""))
    return row


def evidence_from(case: Case, data: dict, kind: str) -> Evidence | None:
    payload: dict = {"case": case}
    try:
        if kind == "positions":
            rows = []
            for row in data.get("positions") or []:
                if not isinstance(row, dict) or not row.get("position"):
                    continue
                row = _clean_unit(dict(row))
                rows.append(TenderPosition.model_validate(row))
            if not rows:
                return None
            payload["positions"] = rows
        elif kind == "qa":
            rows = []
            for row in data.get("qa") or []:
                if not isinstance(row, dict) or not row.get("number") or not row.get("date"):
                    continue
                for change in row.get("quantity_changes") or []:
                    if isinstance(change, dict):
                        _clean_unit(change)
                rows.append(QAAnswer.model_validate(row))
            if not rows:
                return None
            payload["qa"] = rows
        elif kind == "revisions":
            rows = []
            for row in data.get("revisions") or []:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                if row.get("marking") not in {"current", "superseded", "unknown"}:
                    row = dict(row)
                    row["marking"] = "unknown"
                rows.append(SpecRevision.model_validate(row))
            if not rows:
                return None
            payload["revisions"] = rows
        elif kind == "erp":
            materials, searches = [], []
            for row in data.get("materials") or []:
                if not isinstance(row, dict) or not row.get("number"):
                    continue
                if row.get("status") not in {"active", "blocked", "other"}:
                    row = dict(row)
                    raw = str(row.get("status_raw") or row.get("status") or "").casefold()
                    row["status"] = "blocked" if "sperr" in raw or "block" in raw else (
                        "active" if any(w in raw for w in ("aktiv", "frei", "active")) else "other")
                materials.append(Material.model_validate(row))
            for row in data.get("erp_searches") or []:
                if isinstance(row, dict) and row.get("catalog_group"):
                    searches.append(ErpSearch.model_validate(row))
            if not materials and not searches:
                return None
            payload["materials"] = materials
            payload["erp_searches"] = searches
        elif kind == "mail":
            rows = []
            for row in data.get("messages") or []:
                if not isinstance(row, dict) or not row.get("id") or not row.get("date"):
                    continue
                row = dict(row)
                if row.get("kind") not in {
                    "discount_approval", "condition_rules", "metal_index", "technical_release",
                    "packaging_tariffs", "delivery_commitments", "fx_fixing", "setup_pools",
                    "project_bonus", "signing_tan", "other",
                }:
                    row["kind"] = "other"
                rows.append(Message.model_validate(row))
            if not rows:
                return None
            payload["messages"] = rows
        else:
            return None
        return Evidence.model_validate(payload)
    except Exception:
        return None


def parse_extract(text: str) -> dict:
    data = parse_json(text)
    return data.get("facts", data) if isinstance(data.get("facts"), dict) else data


def parse_case(brief: str) -> dict:
    """Labeled fields from the brief text. No website values, no public-scenario constants."""
    found: dict = {"authorized_senders": None}
    if match := _SCENARIO.search(brief or ""):
        found["scenario_date"] = match.group(1)
    elif match := _ISO_ANY.search(brief or ""):
        found["scenario_date"] = match.group(1)
    if match := _REF_LABEL.search(brief or ""):
        found["tender_reference"] = match.group(1).rstrip(".-")
    elif match := _REF_PAREN.search((brief or "")[:800]):
        found["tender_reference"] = match.group(1)
    if match := _DOMAIN_LABEL.search(brief or ""):
        found["bidder_mail_domain"] = match.group(1)
    elif match := _EMAIL_DOMAIN.search(brief or ""):
        found["bidder_mail_domain"] = match.group(1)
    return found


def domain_from_messages(evidence: Evidence | None) -> str | None:
    if evidence is None:
        return None
    for message in evidence.messages:
        local, _, host = (message.sender or "").partition("@")
        if host and local.casefold() == "leitung":
            return host
    for message in evidence.messages:
        _, _, host = (message.sender or "").partition("@")
        if host:
            return host
    return None


def refine_case(evidence: Evidence | None) -> Evidence | None:
    if evidence is None:
        return None
    if evidence.case.bidder_mail_domain and evidence.case.bidder_mail_domain != PENDING_DOMAIN:
        return evidence
    host = domain_from_messages(evidence)
    if not host:
        return evidence
    return evidence.model_copy(
        update={"case": evidence.case.model_copy(update={"bidder_mail_domain": host})})


async def case_from_brief(llm, brief: str, prompt: str) -> Case:
    found = parse_case(brief)
    if found.get("tender_reference") and found.get("scenario_date"):
        found.setdefault("bidder_mail_domain", PENDING_DOMAIN)
        print(f"Case from brief text (domain {found['bidder_mail_domain']})", flush=True)
        return Case.model_validate(found)
    try:
        raw = await llm.complete(
            prompt,
            "Start the reply with {. Copy only fields the brief states.\n\n" + (brief or "")[:2000],
            120, "case", json_object=True)
        data = parse_json(raw)
        for key in ("tender_reference", "scenario_date", "bidder_mail_domain", "authorized_senders"):
            if found.get(key) in (None, "") and data.get(key) not in (None, ""):
                found[key] = data[key]
    except Exception as error:  # noqa: BLE001
        print(f"Case Qwen fallback failed: {error!r}", flush=True)
    if not found.get("tender_reference") or not found.get("scenario_date"):
        raise SystemExit(f"Brief has no tender reference or scenario date: {found}")
    found.setdefault("bidder_mail_domain", PENDING_DOMAIN)
    return Case.model_validate(found)
