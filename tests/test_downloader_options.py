from downloader import ytdlp_base_options


def test_po_token_provider_selects_mweb_by_default(monkeypatch):
    monkeypatch.setenv("YOUTUBE_POT_BASE_URL", "http://pot-provider:4416")
    monkeypatch.delenv("YOUTUBE_PLAYER_CLIENTS", raising=False)

    options = ytdlp_base_options(use_cookies=False)

    assert options["extractor_args"]["youtube"]["player_client"] == ["mweb"]
    assert options["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == [
        "http://pot-provider:4416"
    ]


def test_explicit_player_clients_override_po_token_default(monkeypatch):
    monkeypatch.setenv("YOUTUBE_POT_BASE_URL", "http://pot-provider:4416")
    monkeypatch.setenv("YOUTUBE_PLAYER_CLIENTS", "tv,web_safari")

    options = ytdlp_base_options(use_cookies=False)

    assert options["extractor_args"]["youtube"]["player_client"] == ["tv", "web_safari"]


def test_mweb_is_not_forced_without_po_token_provider(monkeypatch):
    monkeypatch.delenv("YOUTUBE_POT_BASE_URL", raising=False)
    monkeypatch.delenv("YOUTUBE_PLAYER_CLIENTS", raising=False)

    options = ytdlp_base_options(use_cookies=False)

    assert "extractor_args" not in options
