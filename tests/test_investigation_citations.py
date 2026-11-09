"""Evidence citations: what the model is told and what Ollama is allowed to emit."""

from __future__ import annotations

import json

import pytest

from reconcile_investigator.prompts import SYSTEM_PROMPT, format_user_prompt
from reconcile_investigator.providers import InvestigationContext, OllamaProvider
from reconcile_investigator.providers import ollama as ollama_module
from reconcile_investigator.providers.base import citable_evidence_ids
from reconcile_investigator.providers.ollama import NUM_CTX, NUM_PREDICT, build_investigation_schema

PAY_EV = "ev_pay_PAY-1"
INV_EV = "ev_inv_INV-EUR-9"


def _evidence(evidence_id: str, source_type: str, source_id: str, cents: int, currency: str) -> dict:
    return {
        "evidence_id": evidence_id,
        "source_type": source_type,
        "source_id": source_id,
        "amount_cents": cents,
        "currency": currency,
    }


def _context(**overrides) -> InvestigationContext:
    pay = _evidence(PAY_EV, "payment", "PAY-1", 82000, "USD")
    inv = _evidence(INV_EV, "invoice", "INV-EUR-9", 75000, "EUR")
    values = dict(
        investigation_id="i1",
        tenant_id="t1",
        target_type="payment",
        target_id="pay_1",
        target_data={"payment_id": "PAY-1", "evidence": pay},
        evidence_records=[pay, inv],
        candidate_invoices=[{"invoice_number": "INV-EUR-9", "evidence": inv}],
        related_events={"payment": {"payment_id": "PAY-1", "evidence": pay}, "settlements": []},
    )
    values.update(overrides)
    return InvestigationContext(**values)


def test_citable_ids_are_unique_ordered_and_skip_records_without_ids():
    pay = _evidence(PAY_EV, "payment", "PAY-1", 1, "USD")
    context = _context(evidence_records=[pay, {"source_type": "x"}, pay, {"evidence_id": INV_EV}])
    assert citable_evidence_ids(context) == [PAY_EV, INV_EV]


def test_prompt_pairs_each_candidate_invoice_with_its_evidence_id():
    prompt = format_user_prompt(_context())
    section = prompt.split("CANDIDATE INVOICES IN SYSTEM:")[1].split("RELATED FINANCIAL EVENTS")[0]
    candidates = json.loads(section)
    assert candidates == [{"invoice_number": "INV-EUR-9", "evidence_id": INV_EV}]


def test_prompt_lists_citable_ids_and_says_invoice_numbers_are_not_ids():
    prompt = format_user_prompt(_context())
    section = prompt.split("CITABLE EVIDENCE IDS", 1)[1].split("Return your complete")[0]
    listed = json.loads(section[section.index("["):])
    assert [row["evidence_id"] for row in listed] == [PAY_EV, INV_EV]
    assert listed[1]["record"] == "invoice INV-EUR-9"
    assert "invoice number, invoice_id, payment ID or payment reference is NOT an evidence ID" in SYSTEM_PROMPT


def test_related_events_do_not_repeat_full_evidence_records():
    prompt = format_user_prompt(_context())
    section = prompt.split("RELATED FINANCIAL EVENTS", 1)[1].split("IDENTIFIED MISSING")[0]
    related = json.loads(section[section.index("{"):])
    assert related["payment"] == {"payment_id": "PAY-1", "evidence_id": PAY_EV}


def test_schema_limits_every_citation_field_to_retrieved_ids():
    props = build_investigation_schema([PAY_EV, INV_EV])["properties"]
    fields = [
        props["verified_facts"]["items"]["properties"]["evidence_ids"],
        props["conflicting_evidence"]["items"]["properties"]["evidence_ids"],
        props["hypotheses"]["items"]["properties"]["supporting_evidence_ids"],
        props["hypotheses"]["items"]["properties"]["counter_evidence_ids"],
    ]
    for field in fields:
        assert field["items"]["enum"] == [PAY_EV, INV_EV]
    assert props["verified_facts"]["items"]["properties"]["evidence_ids"]["minItems"] == 1


def test_schema_does_not_let_the_model_author_evidence_records():
    schema = build_investigation_schema([PAY_EV])
    assert "supporting_evidence" not in schema["properties"]
    assert schema["additionalProperties"] is False


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"message": {"content": "{}"}}


def _capture_payload(monkeypatch: pytest.MonkeyPatch, context: InvestigationContext) -> dict:
    captured: dict = {}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def post(self, _url, json):
            captured.update(json)
            return _FakeResponse()

    monkeypatch.setattr(ollama_module.httpx, "Client", FakeClient)
    OllamaProvider().generate_investigation(context, "system", "user")
    return captured


def test_provider_sends_the_case_schema_and_enough_context(monkeypatch):
    payload = _capture_payload(monkeypatch, _context())
    enum = payload["format"]["properties"]["verified_facts"]["items"]["properties"]["evidence_ids"]["items"]["enum"]
    assert enum == [PAY_EV, INV_EV]
    assert payload["options"]["num_ctx"] == NUM_CTX
    assert NUM_CTX - NUM_PREDICT > 4305  # measured prompt size for the demo currency-mismatch case


def test_provider_falls_back_to_plain_json_without_evidence(monkeypatch):
    payload = _capture_payload(monkeypatch, _context(evidence_records=[]))
    assert payload["format"] == "json"
