"""Tests for transparent browser cookie resolution."""

from http.cookiejar import Cookie, CookieJar

from bili2am.cookies import resolve_bilibili_cookies


def _make_jar(domain: str, count: int = 1) -> CookieJar:
    jar = CookieJar()
    for i in range(count):
        jar.set_cookie(
            Cookie(
                version=0,
                name=f"k{i}",
                value=f"secret_value_{i}",
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    return jar


def test_cookie_disabled():
    jar, res = resolve_bilibili_cookies(enabled=False, priority=["safari", "chrome"])
    assert jar is None
    assert not res.enabled
    assert res.selected_browser is None
    assert "匿名模式" in res.notice


def test_cookie_priority_fallback_and_no_secret_leak():
    def fake_loader(browser: str) -> CookieJar:
        if browser == "safari":
            raise PermissionError("Operation not permitted")
        if browser == "edge":
            return _make_jar(".example.com", 2)
        if browser == "chrome":
            return _make_jar(".bilibili.com", 3)
        raise RuntimeError("unexpected")

    jar, res = resolve_bilibili_cookies(
        enabled=True,
        priority=["safari", "edge", "chrome"],
        loader=fake_loader,
    )
    assert jar is not None
    assert res.selected_browser == "chrome"
    assert res.bilibili_cookie_count == 3
    assert len(res.attempts) == 3
    assert "Chrome" in res.notice
    assert "secret_value" not in res.notice
    assert "secret_value" not in res.model_dump_json()


def test_cookie_all_fail_graceful_fallback():
    def failing_loader(browser: str) -> CookieJar:
        raise RuntimeError(f"{browser} not installed")

    jar, res = resolve_bilibili_cookies(
        enabled=True,
        priority=["safari", "edge", "chrome"],
        loader=failing_loader,
    )
    assert jar is None
    assert res.selected_browser is None
    assert "匿名模式" in res.notice
    assert len(res.attempts) == 3
