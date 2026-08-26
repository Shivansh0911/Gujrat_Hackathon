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
