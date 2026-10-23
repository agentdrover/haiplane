"""public_reason на границе публикации: JSON, таблицы, длинные токены, userinfo (#2006)."""

from __future__ import annotations

import json


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
    text = json.dumps({"reason": "/srv/k.pem", "note": blob, "tail": [1, 2, 3]})
    out = public_reason(text)
    loaded = json.loads(out)
    assert loaded == {"reason": "<путь>", "note": blob, "tail": [1, 2, 3]}
    assert public_reason(blob) == blob
    long_value = json.dumps({"reason": f"/srv/k.pem {blob}", "ok": True})
    assert json.loads(public_reason(long_value)) == {"reason": "<путь>", "ok": True}


def test_public_reason_takes_the_whole_userinfo_url_and_the_ipv6_host():
    from hub.services.brief_failure import public_reason

    for url in (
        "https://user:p(secret)@host.example/repo",
        "https://user:p(x)y@host.example/repo",
        "https://u:a@b@host.example/r",
        "ssh://git:p[1]:2@host.example:22/r.git",
    ):
        assert public_reason(f"fatal: {url} down") == "fatal: <remote> down", url
    assert public_reason("see https://[2001:db8::1]/repo ok") == "see <remote> ok"
    assert public_reason("|a|https://[::1]:8080/x|b|") == "|a|<remote>|b|", (
        "no @: delimiters"
    )
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


def test_public_reason_replaces_a_bracketed_ipv6_host_whole():
    from hub.services.brief_failure import public_reason

    cred, user = "oauth2:" + "TOKEN", "u:" + "p"
    cases = {
        "https://[2001:db8::1]": "<remote>",
        "https://[::1]": "<remote>",
        "<https://[2001:db8::1]>": "<<remote>>",
        "(https://[::1])": "(<remote>)",
        "|host|https://[2001:db8::1]|down|": "|host|<remote>|down|",
        "https://[::1]:8080/x?t=S ok": "<remote> ok",
        f"https://{cred}@[2001:db8::7]/group/repo.git?token=SECOND ok": ("<remote> ok"),
        f"<https://{cred}@[::1]>": "<<remote>",
        f"|a|https://{user}@[::1]:22/r|b|": "|a|<remote>",
    }
    for text, want in cases.items():
        out = public_reason(text)
        assert out == want, (text, out)
        for left in ("2001", "db8", "TOKEN", "SECOND", "::1", "8080"):
            assert left not in out


def test_public_reason_a_chunk_with_an_at_sign_goes_whole_and_that_costs_neighbours():
    """Утечка хуже испорченного форматирования: кусок до пробела с «@» - целиком."""
    from hub.services.brief_failure import public_reason

    link = "https://github.com/o/r/pull/7"
    run = "https://github.com/o/r/actions/runs/12"
    # Любой разделитель и любое содержимое userinfo: ни фрагмента хоста, пароля, токена.
    pw, at = "p" + "w", "@"
    leaks = (
        f"https://user:{pw}|ass{at}host/repo?token=SECRET ok",
        f"https://user:{pw}{{1}}{at}host/repo?token=SECRET ok",
        f"https://user:{pw}\"q'r{at}host/repo?token=SECRET ok",
        f"https://{at}[::1]/x?token=SECRET ok",
        f"https://user:p[1]<2>{at}[2001:db8::7]/x?token=SECRET ok",
        f"https://user:p(secret){at}host.example/repo?token=SECRET ok",
    )
    for text in leaks:
        out = public_reason(text)
        assert out == "<remote> ok", (text, out)
    # Цена, закреплённая намеренно: соседняя ссылка или поле в том же куске теряются.
    assert public_reason(f"|pr|{link}|alice@host|") == "|pr|<remote>"
    assert public_reason(f'{{"provenance":"{run}","actor":"a@b"}}') == (
        '{"provenance":"<remote>'
    )
    assert public_reason(f"{link}.|a@b") == "<remote>"
    out = public_reason(f'{{"link":"{link}","detail":"/srv/key.pem","e":"a@b"}}')
    assert out == '{"link":"<remote>', "ссылка и «@» в одном куске без пробела"
    # Без «@» кусок живёт по правилам с разделителями.
    assert public_reason(f"|pr|{link}|success|") == f"|pr|{link}|success|"
    ok = f'{{"provenance":"{run}","ok":true}}'
    assert public_reason(ok) == ok
    assert public_reason(f"{link}, затем mail me@host") == f"{link}, затем mail me@host"


def test_public_reason_a_path_with_a_quote_inside_is_taken_whole_or_to_whitespace():
    from hub.services.brief_failure import public_reason

    assert public_reason("'/srv/a'b c' tail") == "<путь> tail"
    assert public_reason("fatal: '/srv/a'b c' tail x") == "fatal: <путь> tail x"
    assert public_reason('"/srv/reviewer/pri\\"vate.pem" failed') == "<путь> failed"
    # Неясно, где кончается имя: берём до пробела.
    assert public_reason("'/srv/a'b secret-tail") == "<путь> secret-tail"
    assert public_reason("fatal: '/srv/x' and 'y' ok") == "fatal: <путь> and 'y' ok"


def test_public_reason_replaces_a_bracketed_ipv6_scp_address():
    from hub.services.brief_failure import public_reason

    assert public_reason("git@[::1]:repo ok") == "<remote> ok"
    assert public_reason("(git@[2001:db8::1]:o/r.git?t=S)") == "(<remote>)"
    assert public_reason("|a|git@[::1]:r|b|") == "|a|<remote>|b|"


def test_public_reason_json_with_an_escaped_quote_in_the_path():
    from hub.services.brief_failure import public_reason

    out = public_reason(r'{"reason":"/srv/reviewer/pri\"vate.pem","ok":true}')
    assert json.loads(out) == {"reason": "<путь>", "ok": True}
    assert "vate" not in out and "reviewer" not in out


def test_public_reason_a_quote_inside_a_name_followed_by_a_delimiter_does_not_end_it():
    from hub.services.brief_failure import public_reason

    forms = (
        "'/srv/a',b c' tail",
        "'/srv/a')b c' tail",
        "'/srv/a';b c' tail",
        "'/srv/a'|b c' tail",
        "'/srv/a'>b c' tail",
        "'/srv/a':b c' tail",
        "'/srv/a'}b c' tail",
        "'/srv/a\\'b c' tail",
        "'/srv/a\\' b c' tail",
        '"/srv/a\\" b c" tail',
        '"/srv/a\\"b c" tail',
        '"/srv/a",b c" tail',
        "`/srv/a`,b c` tail",
    )
    for text in forms:
        out = public_reason(text)
        assert out == "<путь> tail", (text, out)
    # Неясно, где конец: до пробела, знаки после пути теряются (цена известна).
    assert public_reason("'/srv/a',b c d") == "<путь> c d"
    assert public_reason("(see '/srv/x') ok") == "(see <путь> ok"
    # Обычные случаи не затронуты.
    assert public_reason("fatal: '/srv/x' and 'y' ok") == "fatal: <путь> and 'y' ok"
    assert (
        public_reason("fatal: '/srv/private clone/.git' down") == "fatal: <путь> down"
    )
