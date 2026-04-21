from services.gallery_service import _build_like_where_and_args


def test_build_like_where_and_args():
    where_sql, args = _build_like_where_and_args(["abc", "def"])
    assert "AND" in where_sql
    assert len(args) == 6
