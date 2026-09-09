from agent_bridge.companion.theme import DARK, LIGHT, build_stylesheet, resolve_theme


def test_explicit_theme_overrides_system() -> None:
    assert resolve_theme("light", system_dark=True) is LIGHT
    assert resolve_theme("dark", system_dark=False) is DARK


def test_system_theme_follows_color_scheme() -> None:
    assert resolve_theme("system", system_dark=False) is LIGHT
    assert resolve_theme("system", system_dark=True) is DARK


def test_stylesheet_uses_semantic_theme_colors() -> None:
    light_stylesheet = build_stylesheet(LIGHT)
    dark_stylesheet = build_stylesheet(DARK)

    assert LIGHT.background in light_stylesheet
    assert LIGHT.primary in light_stylesheet
    assert "customTitleBar" in light_stylesheet
    assert "titlebarRole" in light_stylesheet
    assert DARK.background in dark_stylesheet
    assert DARK.error in dark_stylesheet
