"""Tests for group_warnings_by_product (app/core/services/compatibility_service.py).

analyze() reports each clashing ingredient pair as its own warning. The shelf
and compare endpoints pass its output through group_warnings_by_product, so the
user sees one warning per clashing product with every pair listed in details.

Called directly with hand-built warnings: these are about how warnings merge,
and building them by hand pins each severity and order exactly.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

from app.core.services.compatibility_service import group_warnings_by_product
from app.schemas import ConflictDetail, WarningAlert

SERUM = "Peptide Serum"
TONER = "Glycolic Toner"


def clash(product, ingredient, other, severity="Medium", alert_type="Active Routine Clash"):
    """One pair, shaped as analyze() emits it: a warning carrying itself as its
    only detail."""
    message = f"Conflict with {product}: {ingredient} with {other}."
    return WarningAlert(
        alert_type=alert_type, severity=severity, message=message,
        conflicting_product=product,
        details=[ConflictDetail(alert_type=alert_type, severity=severity,
                                ingredient=ingredient, conflicting_ingredient=other,
                                message=message)],
    )


SKIN_ALERT = WarningAlert(alert_type="Skin Type Conflict", severity="High",
                          message="Personalized Alert: Salicylic Acid ...")


def test_merges_every_pair_with_one_product_into_one_warning():
    """Returns a single warning for three pairs that name the same product, with
    all three pairs in its details."""
    warnings = [clash(SERUM, "Salicylic Acid", p) for p in ("Peptide A", "Peptide B", "Peptide C")]

    [merged] = group_warnings_by_product(warnings)

    assert merged.conflicting_product == SERUM
    assert [d.conflicting_ingredient for d in merged.details] == ["Peptide A", "Peptide B", "Peptide C"]


def test_merged_warning_takes_the_most_severe_pairs_grade_and_type():
    """Returns the merged warning graded and typed as its most severe pair, with
    the details ordered most severe first, so the card sorts and colours like
    its worst clash."""
    warnings = [
        clash(SERUM, "Salicylic Acid", "Peptide A", severity="Low"),
        clash(SERUM, "Salicylic Acid", "Retinol", severity="High",
              alert_type="Chemical Interaction Warning"),
        clash(SERUM, "Salicylic Acid", "Peptide B", severity="Medium"),
    ]

    [merged] = group_warnings_by_product(warnings)

    assert merged.severity == "High"
    assert merged.alert_type == "Chemical Interaction Warning"
    assert [d.severity for d in merged.details] == ["High", "Medium", "Low"]


def test_pairs_of_equal_severity_keep_the_order_they_were_found_in():
    """Returns pairs of the same severity in the order analyze() produced them,
    so the details list does not reshuffle between requests."""
    warnings = [clash(SERUM, "Salicylic Acid", p) for p in ("Zeta", "Alpha", "Mu")]

    [merged] = group_warnings_by_product(warnings)

    assert [d.conflicting_ingredient for d in merged.details] == ["Zeta", "Alpha", "Mu"]


def test_summary_names_each_pair_grouped_by_the_checked_ingredient():
    """Returns a summary message that counts the pairs and lists, for each of
    the checked product's ingredients, what it clashes with."""
    warnings = [
        clash(SERUM, "Salicylic Acid", "Peptide A"),
        clash(SERUM, "Salicylic Acid", "Peptide B"),
        clash(SERUM, "Glycolic Acid", "Peptide A"),
    ]

    [merged] = group_warnings_by_product(warnings)

    assert merged.message == (
        "Conflict with Peptide Serum: 3 ingredient clashes. "
        "Salicylic Acid with Peptide A and Peptide B; Glycolic Acid with Peptide A."
    )


def test_summary_counts_a_pair_reported_by_both_rule_tables_once():
    """Returns "1 ingredient clash" when the same pair arrives from both rule
    tables, which happens when the category rule is graded above the curated
    one and so is not suppressed. The pair is one clash, listed once."""
    warnings = [
        clash(SERUM, "Retinol", "Salicylic Acid", severity="Low",
              alert_type="Chemical Interaction Warning"),
        clash(SERUM, "Retinol", "Salicylic Acid", severity="High"),
    ]

    [merged] = group_warnings_by_product(warnings)

    assert merged.message == "Conflict with Peptide Serum: 1 ingredient clash. Retinol with Salicylic Acid."
    assert len(merged.details) == 2


def test_a_product_with_one_pair_keeps_its_original_warning():
    """Returns the original warning unchanged, message included, for a product
    that clashes on a single pair, since there is nothing to merge."""
    single = clash(TONER, "Retinol", "Glycolic Acid")

    assert group_warnings_by_product([single]) == [single]


def test_different_products_are_never_merged_together():
    """Returns one warning per product: pairs naming the serum merge into the
    serum's warning and never into the toner's."""
    warnings = [
        clash(SERUM, "Salicylic Acid", "Peptide A"),
        clash(TONER, "Salicylic Acid", "Glycolic Acid"),
        clash(SERUM, "Salicylic Acid", "Peptide B"),
    ]

    grouped = group_warnings_by_product(warnings)

    assert [(w.conflicting_product, len(w.details)) for w in grouped] == [(SERUM, 2), (TONER, 1)]


def test_skin_type_alerts_pass_through_in_place():
    """Returns a skin-type alert, which names no other product, untouched and
    in its original position, with each merged warning standing where its
    product first appeared."""
    warnings = [
        SKIN_ALERT,
        clash(SERUM, "Salicylic Acid", "Peptide A"),
        clash(TONER, "Salicylic Acid", "Glycolic Acid"),
        clash(SERUM, "Salicylic Acid", "Peptide B"),
    ]

    grouped = group_warnings_by_product(warnings)

    assert grouped[0] is SKIN_ALERT
    assert [w.conflicting_product for w in grouped] == [None, SERUM, TONER]


def test_no_warnings_stays_no_warnings():
    """Returns an empty list for an empty list, so a clean scan stays clean."""
    assert group_warnings_by_product([]) == []
