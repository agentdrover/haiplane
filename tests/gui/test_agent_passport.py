import pytest
from playwright.sync_api import expect


@pytest.mark.parametrize("width", [375, 1280])
def test_agent_passport_disclosure_is_readable(page, stand, width):
    page.set_viewport_size({"width": width, "height": 1000})
    before = stand.query("SELECT id,status FROM tasks ORDER BY id")
    page.goto(f"{stand.url}/projects")
    card = page.locator(".project-card").filter(has_text="Паспорт агента").first
    outer = card.get_by_text("Паспорт агента", exact=True)
    outer.focus()
    page.keyboard.press("Enter")
    link = card.get_by_role("link", name="Читать паспорт")
    link.focus()
    page.keyboard.press("Enter")
    passport = card.get_by_role("region", name="Паспорт агента")
    expect(passport).to_be_visible()
    inner = passport.get_by_text("Данные и источники", exact=True)
    inner.focus()
    page.keyboard.press("Enter")
    data = passport.locator("pre")
    expect(data).to_be_visible()
    expect(data).to_contain_text("effective policy")
    assert data.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
    assert stand.query("SELECT id,status FROM tasks ORDER BY id") == before
