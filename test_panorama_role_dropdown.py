from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_panorama_groups_roles_and_does_not_repeat_family_in_option_text() -> None:
    page = (ROOT / "qianduan" / "html-main2" / "panorama.html").read_text(
        encoding="utf-8"
    )

    assert "function renderRoleOptions(roles)" in page
    assert '<optgroup label="${esc(group.familyName)}">' in page
    assert ">${esc(role.role_name)}</option>" in page
    assert "prefix + role.role_name" not in page
    assert 'class="wide role-filter"' in page


def test_panorama_role_family_order_covers_all_production_families() -> None:
    page = (ROOT / "qianduan" / "html-main2" / "panorama.html").read_text(
        encoding="utf-8"
    )
    for family_id in (
        "digital_product",
        "ai",
        "data",
        "backend",
        "client",
        "quality",
        "chip",
        "electronics",
        "infrastructure",
        "intelligent_systems",
        "enterprise_solutions",
    ):
        assert f'"{family_id}"' in page
