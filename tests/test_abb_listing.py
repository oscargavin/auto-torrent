import base64

from auto_torrent.abb import parse_listing
from auto_torrent.config import ABB_BASE_URL

POST = """
<div class="post">
  <div class="postTitle"><h2><a href="/abss/frederica-georgette-heyer/">Frederica - Georgette Heyer</a></h2></div>
  <div class="postContent">
    <p style="text-align:center;">
      Format: <span>MP3</span><br/>Bitrate: <span>64</span> Kbps<br/>
      File Size: <span>371.53</span> MBs<br/>Posted: 12 Mar 2021<br/>
    </p>
  </div>
</div>
"""


def hidden(markup: str) -> str:
    """How the site ships some listing pages: the post markup base64'd into a
    hidden div for its own JS to expand."""
    inner = markup.strip().removeprefix('<div class="post">').removesuffix("</div>").strip()
    encoded = base64.b64encode(inner.encode()).decode()
    return f'<div class="post re-ab" style="display:none;">{encoded}</div>'


class TestParseListing:
    def test_reads_a_plain_post(self):
        results = parse_listing(POST)
        assert len(results) == 1
        assert results[0].title == "Frederica - Georgette Heyer"
        assert results[0].link == f"{ABB_BASE_URL}/abss/frederica-georgette-heyer/"

    def test_reads_metadata(self):
        r = parse_listing(POST)[0]
        assert r.format == "MP3"
        assert r.bitrate == "64 Kbps"
        assert r.file_size == "371.53 MBs"
        assert r.posted == "12 Mar 2021"

    def test_reads_a_base64_hidden_post(self):
        results = parse_listing(hidden(POST))
        assert len(results) == 1
        assert results[0].title == "Frederica - Georgette Heyer"
        assert results[0].format == "MP3"

    def test_mixed_pages_return_both(self):
        assert len(parse_listing(POST + hidden(POST))) == 2

    def test_skips_a_post_it_cannot_decode(self):
        assert parse_listing('<div class="post re-ab">not base64 at all!</div>') == []
