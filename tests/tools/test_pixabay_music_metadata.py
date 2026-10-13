"""Pixabay music tool: track-title parsing and blocked-IP diagnostics.

Two defects are covered here:

1. Tracks whose Pixabay payload has no `name` field were all reported as
   the literal title "Unknown", which destroys selection and provenance.
2. Requests from blocked networks (datacenter/CI IPs) return HTTP 403 with
   no explanation, sending agents hunting for a parser bug. The tool must
   name the cause and the zero-key alternatives.
"""

import urllib.error

from tools.audio.pixabay_music import PixabayMusic


def _item(**overrides) -> dict:
    item = {
        "sources": {"src": "https://cdn.pixabay.com/audio/2024/01/cinematic-42.mp3"},
        "duration": 90,
        "user": {"username": "artist"},
        "id": 42,
    }
    item.update(overrides)
    return item


def test_title_from_explicit_name():
    title = PixabayMusic._track_title(
        _item(name="Slow Motion"), {"src": "x.mp3"}, "https://cdn.pixabay.com/a.mp3"
    )
    assert title == "Slow Motion"


def test_title_falls_back_to_de_slugged_filename():
    sources = {"src": "https://cdn.pixabay.com/audio/x.mp3", "filename": "cinematic-documentary-146983.mp3"}
    title = PixabayMusic._track_title(_item(), sources, sources["src"])
    assert title == "Cinematic Documentary"


def test_title_falls_back_to_cdn_url_basename():
    url = "https://cdn.pixabay.com/audio/2024/01/ambient-rain-88231.mp3"
    title = PixabayMusic._track_title(_item(), {}, url)
    assert title == "Ambient Rain"


def test_title_never_returns_empty_string():
    assert PixabayMusic._track_title(_item(), {}, "https://cdn.pixabay.com/") == "Unknown"


def test_blocked_ip_error_names_cause_and_alternatives():
    message = PixabayMusic._describe_failure(urllib.error.HTTPError("u", 403, "Forbidden", {}, None))

    assert "403" in message
    assert "music_library" in message
    assert "FREESOUND_API_KEY" in message


def test_rate_limit_error_is_treated_as_blocked():
    message = PixabayMusic._describe_failure(urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None))

    assert "429" in message


def test_other_errors_keep_original_text():
    message = PixabayMusic._describe_failure(ValueError("boom"))

    assert "boom" in message


def test_parse_bootstrap_keeps_language_and_readable_titles():
    html = '<script>window.__BOOTSTRAP_URL__ = "/music/api/abc";</script>'
    payload = {
        "page": {
            "results": [
                {
                    "sources": {"src": "https://cdn.pixabay.com/audio/happy-uplifting-12.mp3"},
                    "duration": 60,
                    "language": "en",
                    "user": {"username": "who"},
                }
            ]
        }
    }

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            import json

            return json.dumps(payload).encode()

    class _Opener:
        def open(self, request, timeout=None):
            return _Response()

    tracks = PixabayMusic()._parse_bootstrap(html, "https://pixabay.com/music/", _Opener())

    assert len(tracks) == 1
    assert tracks[0]["title"] == "Happy Uplifting"
    assert tracks[0]["language"] == "en"
