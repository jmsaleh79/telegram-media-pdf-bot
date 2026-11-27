import logging

import bot


def test_httpx_info_logs_do_not_expose_telegram_api_urls(caplog):
    logger = logging.getLogger("httpx")
    caplog.set_level(logging.INFO)

    logger.info(
        'HTTP Request: POST https://api.telegram.org/bot123456:secret-token/sendMessage "HTTP/1.1 200 OK"'
    )

    assert "secret-token" not in caplog.text
