from agent_relances.outbox import reference, with_reference


def test_with_reference_is_idempotent_and_replaces_any_other_tag():
    ref = reference("R", 42)
    assert with_reference("Rappel", ref) == "Rappel [Réf. R-00042]"
    assert with_reference("Rappel [Réf. R-00042]", ref) == "Rappel [Réf. R-00042]"
    assert with_reference("Rappel [Réf. E-00007] facture", ref) == "Rappel facture [Réf. R-00042]"
