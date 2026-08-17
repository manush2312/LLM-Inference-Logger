"""Settings-derived connection URLs.

`redis_url` grew a password for the hosted deployment, where an unauthenticated
Redis on a routable address is not survivable. The quoting is the part worth
testing: a generated secret routinely contains `@`, `:` or `/`, and interpolating
one raw produces a URL that parses as a *different host*. The resulting failure
looks like a networking or DNS problem, so it is expensive to diagnose and cheap
to prevent.
"""

from __future__ import annotations

from app.core.config import Settings


def settings(**overrides: object) -> Settings:
    # _env_file=None keeps this hermetic: the developer's own .env must not
    # decide whether this passes.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_redis_url_has_no_auth_section_when_no_password() -> None:
    assert settings(redis_host="redis", redis_port=6379).redis_url == "redis://redis:6379/0"


def test_redis_url_includes_the_password() -> None:
    url = settings(redis_host="redis", redis_password="s3cret").redis_url
    assert url == "redis://:s3cret@redis:6379/0"


def test_password_metacharacters_are_percent_encoded() -> None:
    # '@' would otherwise end the userinfo section early and make "b" the host.
    url = settings(redis_host="redis", redis_password="a@b:c/d").redis_url
    assert url == "redis://:a%40b%3Ac%2Fd@redis:6379/0"
    # The real host must survive, which is the thing raw interpolation breaks.
    assert url.endswith("@redis:6379/0")


def test_empty_password_is_treated_as_absent() -> None:
    # Compose passes through an unset variable as "", so this is the common
    # accident rather than a hypothetical one.
    assert settings(redis_host="redis", redis_password="").redis_url == "redis://redis:6379/0"
