from orizonctl.envfile import MARKER, missing_keys, parse, quote_if_needed, render


def test_parse_strips_quotes_and_skips_comments():
    text = 'A=1\n# B=2\nC="three"\nD=\'four\'\nexport E=5\nF=[\"x\",\"y\"]\n'
    assert parse(text) == {"A": "1", "C": "three", "D": "four", "E": "5", "F": '["x","y"]'}


def test_render_replaces_active_lines_in_place_and_keeps_comments():
    base = "# header\nA=old\n# a comment about B\nB=keep\n"
    out = render(base, {"A": "new"})
    assert out == "# header\nA=new\n# a comment about B\nB=keep\n"


def test_render_uncomments_a_commented_key_instead_of_appending():
    base = "# LLM_API_URL=\nX=1\n"
    out = render(base, {"LLM_API_URL": "https://api.example.com"})
    assert out == "LLM_API_URL=https://api.example.com\nX=1\n"
    assert MARKER not in out


def test_render_appends_unknown_keys_under_marker_once():
    base = "X=1\n"
    out = render(base, {"NEW_A": "a", "NEW_B": "b"})
    assert out.count(MARKER) == 1
    assert "NEW_A=a\nNEW_B=b\n" in out
    again = render(out, {"NEW_C": "c"})
    assert again.count(MARKER) == 1
    assert again.endswith("NEW_B=b\nNEW_C=c\n")


def test_render_prefers_active_line_over_commented_duplicate():
    base = "# MODELS_PATH=/old/commented\nMODELS_PATH=/active\n"
    out = render(base, {"MODELS_PATH": "/new"})
    assert out == "# MODELS_PATH=/old/commented\nMODELS_PATH=/new\n"


def test_quote_when_compose_would_mangle():
    assert quote_if_needed("plain") == "plain"
    assert quote_if_needed('["https://a","http://b"]') == '["https://a","http://b"]'
    assert quote_if_needed("has #hash") == '"has #hash"'
    assert quote_if_needed("") == ""


def test_missing_keys_reports_template_growth():
    assert missing_keys("A=1\nB=2\n# C=3\n", "A=x\n") == ["B"]
