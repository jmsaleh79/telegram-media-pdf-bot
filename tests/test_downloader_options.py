from downloader import ytdlp_base_options


def test_po_token_provider_selects_recommended_mweb_client(monkeypatch):
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


def test_ytdlp_selects_client_automatically_without_provider_or_override(monkeypatch):
    monkeypatch.delenv("YOUTUBE_POT_BASE_URL", raising=False)
    monkeypatch.delenv("YOUTUBE_PLAYER_CLIENTS", raising=False)

    options = ytdlp_base_options(use_cookies=False)

    assert "extractor_args" not in options


def test_cookies_keep_ytdlp_automatic_client_selection(tmp_path, monkeypatch):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("YOUTUBE_COOKIES_FILE", str(cookies))
    monkeypatch.delenv("YOUTUBE_POT_BASE_URL", raising=False)
    monkeypatch.delenv("YOUTUBE_PLAYER_CLIENTS", raising=False)

    options = ytdlp_base_options(scratch_dir=tmp_path / "download")

    assert options["cookiefile"] == str(tmp_path / "download" / "cookies.txt")
    assert "extractor_args" not in options
