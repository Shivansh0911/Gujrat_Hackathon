import pytest

import logging

from services.common.redact import RedactingFilter, redact


def test_userinfo_credentials_are_masked():
    out = redact("connecting to rtsp://admin:Hunter2@10.1.2.3:554/cam1")
    assert "Hunter2" not in out
    assert "admin" not in out


def test_bare_upstream_rtsp_url_is_masked():
    # The path itself is sensitive infrastructure detail under §6, credentials or not.
    out = redact("opening rtsp://live.example.test:8554/stream/17 now")
    assert "8554" not in out
    assert "[REDACTED]" in out
    assert out.startswith("opening ") and out.endswith(" now")


def test_keyed_secrets_are_masked_but_key_survives():
    out = redact('{"password": "s3cr3t", "api_key": "abc123", "user": "ops"}')
    assert "s3cr3t" not in out and "abc123" not in out
    assert "password" in out and "api_key" in out
    assert "ops" in out  # non-secret fields must remain useful for debugging


def test_filter_scrubs_interpolated_args(caplog):
    logger = logging.getLogger("test.redact")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="test.redact"):
        logger.info("url=%s", "rtsp://u:p@host:554/s")
    assert "p@host" not in caplog.text
    assert "554" not in caplog.text


# --------------------------------------------- gaps found by the observability work


@pytest.mark.parametrize(
    "line,secret",
    [
        ("connecting with SETU_GATEWAY_ACCESS_CODE=ABCD-1234-EFGH", "ABCD-1234-EFGH"),
        ("SETU_JWT_SECRET=supersecretvalue", "supersecretvalue"),
        ("SETU_APP_DB_PASSWORD=p4ssw0rd", "p4ssw0rd"),
        ("SETU_EVIDENCE_SIGNING_KEY=deadbeefcafe", "deadbeefcafe"),
    ],
    ids=["gateway-access-code", "jwt-secret", "db-password", "signing-key"],
)
def test_screaming_snake_environment_names_are_redacted(line: str, secret: str) -> None:
    """`\b` does not fire inside SETU_GATEWAY_ACCESS_CODE -- underscore is a word
    character, so there is no boundary before ACCESS.

    That spelling is precisely how the credential appears in a line echoing
    configuration, which is the line most likely to be logged while debugging a feed at
    2am. The filter did not cover it, which a test asserting a secret did not survive a
    log line discovered.
    """
    assert secret not in redact(line)


@pytest.mark.parametrize(
    "line,secret",
    [
        ("access_code=5ZYJ-S7M5-27SF", "5ZYJ-S7M5-27SF"),
        ("gateway-access-code: KG56-LMWA-KNLR", "KG56-LMWA-KNLR"),
        ('{"access_code": "ABCD-1234"}', "ABCD-1234"),
    ],
    ids=["snake", "kebab-header", "json"],
)
def test_the_gateway_access_code_is_redacted_in_every_spelling(line: str, secret: str) -> None:
    """The credential this project rotates most often was absent from the key list."""
    assert secret not in redact(line)


def test_a_bearer_token_is_redacted_not_merely_the_word_bearer() -> None:
    """The key rule treated `authorization` as the key and `Bearer` as the value, so it
    redacted the literal word and left the token beside it.

    `Authorization: [REDACTED] eyJhbGciOi...` looks redacted at a glance, which is worse
    than looking obviously unredacted.
    """
    token = "eyJhbGciOiJIUzI1NiJ9.payload.signature"
    assert token not in redact(f"Authorization: Bearer {token}")


def test_a_basic_credential_is_redacted() -> None:
    blob = "YWRtaW46aHVudGVyMg=="
    assert blob not in redact(f"Authorization: Basic {blob}")


def test_an_api_key_token_is_redacted() -> None:
    assert "setu_sk_abcdefghijkl" not in redact("api_key=setu_sk_abcdefghijkl")


def test_an_ordinary_line_is_left_alone() -> None:
    """Redaction is deliberately aggressive; it still must not eat the logs."""
    line = "camera cam22 produced 148 frames at 15.2 fps, 3 discontinuities"
    assert redact(line) == line
