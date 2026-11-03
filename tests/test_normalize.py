from reconcile.normalize import (
    canonical_reference,
    extract_invoice_references,
)

INVOICES = ("INV-1001", "INV-1002", "INV-10010")


def test_extracts_reference_from_free_text():
    result = extract_invoice_references(
        ["Payment for INV-1001"], INVOICES
    )
    assert result == ("INV-1001",)


def test_accepts_spaces_and_different_case():
    result = extract_invoice_references(
        ["paid inv 1001"], INVOICES
    )
    assert result == ("INV-1001",)


def test_extracts_multiple_references_in_text_order():
    result = extract_invoice_references(
        ["Covering INV-1002 and INV-1001"], INVOICES
    )
    assert result == ("INV-1002", "INV-1001")


def test_does_not_confuse_similar_invoice_numbers():
    result = extract_invoice_references(
        ["Payment for INV-10010"], INVOICES
    )
    assert result == ("INV-10010",)


def test_rejects_reference_inside_another_token():
    result = extract_invoice_references(
        ["Reference XINV-1001"], INVOICES
    )
    assert result == ()


def test_ignores_empty_text_and_unknown_numbers():
    result = extract_invoice_references(
        [None, "", "Paid invoice 9999"], INVOICES
    )
    assert result == ()


def test_existing_canonical_reference_still_works():
    assert canonical_reference("inv 1001") == "INV1001"


def test_reference_extraction_does_not_match_amount_with_decimal():
    result = extract_invoice_references(
        ["Paid INV 10.01 USD"], ("INV-1001",)
    )
    assert result == ()


def test_reference_extraction_allows_separators_between_groups_not_inside_digits():
    invoices = ("INV-1001", "INV-2026-01")
    # Allowed separators between letter and digit groups
    assert extract_invoice_references(["INV 1001"], invoices) == ("INV-1001",)
    assert extract_invoice_references(["INV-1001"], invoices) == ("INV-1001",)
    assert extract_invoice_references(["INV_1001"], invoices) == ("INV-1001",)
    assert extract_invoice_references(["INV1001"], invoices) == ("INV-1001",)
    # Not allowed inside digit groups
    assert extract_invoice_references(["Paid INV 10.01 USD"], invoices) == ()
    assert extract_invoice_references(["INV 10-01"], invoices) == ()
