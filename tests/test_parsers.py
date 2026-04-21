from plugin.parsers import build_random_params, parse_rating_text


def test_build_random_params_defaults_to_q():
    assert build_random_params("1girl") == {"q": "1girl"}


def test_build_random_params_cat_hint():
    assert build_random_params("风景:3,人像:1") == {"cat": "风景:3,人像:1"}


def test_parse_rating_text():
    score, note = parse_rating_text("4.5 光影舒服")
    assert score == 4.5
    assert note == "光影舒服"
