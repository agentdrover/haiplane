"""public_reason на границе публикации: JSON, таблицы, длинные токены, userinfo (#2006)."""

from __future__ import annotations

import json

import pytest


def test_public_reason_keeps_json_valid_with_the_marker_inside_the_quotes():
    from hub.services.brief_failure import public_reason

    one = public_reason('{"reason":"/srv/key.pem","ok":true}')
    assert json.loads(one) == {"reason": "<путь>", "ok": True}
    many = public_reason(
        '{"a": ["/srv/my dir/x.pem", "ok"], "reason": "/home/u/k", "n": 1, '
        '"detail": "open /srv/key.pem"}'
    )
    assert json.loads(many) == {
        "a": ["<путь>", "ok"],
        "reason": "<путь>",
        "n": 1,
        "detail": "open <путь>",
    }


def test_public_reason_ends_urls_and_paths_at_table_and_bracket_edges():
    from hub.services.brief_failure import public_reason

    link = "https://github.com/o/r/pull/7"
    assert public_reason(f"|pr|{link}|success|") == f"|pr|{link}|success|"
    assert public_reason("|x|https://evil.io/a|ok|") == "|x|<remote>|ok|"
    assert public_reason("|f|/srv/a/b.pem|ok|") == "|f|<путь>|ok|"
    assert public_reason("[a](https://evil.io/a) {/srv/x}") == "[a](<remote>) {<путь>}"
    assert public_reason("см. https://evil.io/a, затем /srv/x; ок") == (
        "см. <remote>, затем <путь>; ок"
    )
    assert public_reason("https://evil.io/a,b/c") == "<remote>"


def test_public_reason_does_not_cut_a_long_token():
    from hub.services.brief_failure import public_reason

    blob = "x" * 4500
    text = json.dumps({"reason": f"/srv/k.pem {blob}", "tail": [1, 2, 3]})
    out = public_reason(text)
    loaded = json.loads(out)
    assert loaded["tail"] == [1, 2, 3] and loaded["reason"].endswith(blob)
    assert public_reason(blob) == blob


def test_public_reason_takes_the_whole_userinfo_url_and_the_ipv6_host():
    from hub.services.brief_failure import public_reason

    for url in (
        "https://user:p(secret)@host.example/repo",
        'https://user:p|"q)x@host.example/repo',
        "https://u:a@b@host.example/r",
        "ssh://git:p[1]{2}<3>@host.example:22/r.git",
    ):
        assert public_reason(f"fatal: {url} down") == "fatal: <remote> down", url
    assert public_reason("see https://[2001:db8::1]/repo ok") == "see <remote> ok"
    assert public_reason("|a|https://[::1]:8080/x|b|") == "|a|<remote>|b|"
    assert "secret" not in public_reason("https://user:p(secret)@host.example/repo)")


def test_public_reason_swallows_the_tail_after_the_match_cap():
    from hub.services.brief_failure import public_reason

    far = "a" * 4500
    for text, marker in (
        (f"x https://evil.io/{far}?token=SECRET y", "x <remote> y"),
        (f"x /srv/{far}?token=SECRET y", "x <путь> y"),
        (f"x git@github.com:o/{far}?token=SECRET y", "x <remote> y"),
        (
            f'{{"u":"https://evil.io/{far}?token=SECRET","ok":true}}',
            '{"u":"<remote>","ok":true}',
        ),
    ):
        out = public_reason(text)
        assert "SECRET" not in out and out == marker, out[:80]
    near = "https://github.com/o/r/pull/7"
    assert public_reason(f"{near}, ok") == f"{near}, ok"


@pytest.mark.xfail(strict=True, reason="#2006: escaped quote — separate task")
def test_public_reason_json_with_an_escaped_quote_in_the_path():
    from hub.services.brief_failure import public_reason

    out = public_reason(r'{"reason":"/srv/a\"b.pem","ok":true}')
    assert json.loads(out) == {"reason": "<путь>", "ok": True}
