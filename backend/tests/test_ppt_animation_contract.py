from backend.agents.builtin_workers import (
    _apply_ppt_animation_contract,
    _deck_theme_css,
)


def test_animation_contract_adds_staged_reveals_and_accessibility():
    fragment = """
    <section class="slide" style="display:flex; width:100vw; color:red">
      <div class="kicker">PART 01</div>
      <h1>标题</h1>
      <p class="lead">正文</p>
      <div class="visual"><svg viewBox="0 0 10 10"></svg></div>
    </section>
    """

    result = _apply_ppt_animation_contract(fragment, 3)

    assert 'data-slide="3"' in result
    assert 'aria-label="第 3 页"' in result
    assert "display:flex" not in result
    assert "width:100vw" not in result
    assert "color:red" in result
    assert result.count("reveal") >= 4
    assert "--reveal-order:1" in result
    assert '<div class="page-no">3</div>' in result


def test_animation_contract_reuses_existing_page_number():
    fragment = '<section class="slide"><h1>标题</h1><div class="page-no">9/9</div></section>'

    result = _apply_ppt_animation_contract(fragment, 2)

    assert result.count('class="page-no"') == 1
    assert '<div class="page-no">2</div>' in result


def test_all_ppt_animation_themes_have_slide_visual_rules():
    for theme in ("dark-tech", "warm-paper", "clean-white", "cyber-red", "gradient-dark"):
        css = _deck_theme_css(theme)
        assert ".slide" in css
        assert ".visual" in css
        assert ".page-no" in css
