"""_probe_seeds_batch against a fake libtorrent session and a fake clock."""
from types import SimpleNamespace
from unittest.mock import patch

from auto_torrent import cli


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def _probe(peers_at: dict[str, float], timeout: int = 10, **kw):
    """Each magnet shows one peer from its time in `peers_at` onwards."""
    clock = _Clock()
    polls: list[float] = []

    class Handle:
        def __init__(self, magnet: str) -> None:
            self.magnet = magnet

        def status(self):
            at = peers_at.get(self.magnet)
            return SimpleNamespace(num_peers=1 if at is not None and clock.now >= at else 0)

    class Session:
        def __init__(self, settings) -> None:
            pass

        def add_dht_router(self, host, port) -> None:
            pass

        def add_torrent(self, params):
            return Handle(params.magnet)

        def post_torrent_updates(self) -> None:
            polls.append(clock.now)

        def remove_torrent(self, handle) -> None:
            pass

    def parse(magnet: str):
        return SimpleNamespace(magnet=magnet, save_path="", flags=0, trackers=[])

    fake_lt = SimpleNamespace(
        session=Session,
        parse_magnet_uri=parse,
        torrent_flags=SimpleNamespace(upload_mode=1),
        torrent_handle=Handle,
    )
    with (
        patch.object(cli, "lt", fake_lt),
        patch.object(cli.time, "monotonic", clock.monotonic),
        patch.object(cli.time, "sleep", clock.sleep),
    ):
        result = cli._probe_seeds_batch(list(peers_at), timeout, **kw)
    return result, polls[-1]


def test_by_default_stops_at_the_first_peer():
    result, last = _probe({"a": 1.0, "b": 3.0})
    assert result == {"a": 1, "b": 0}
    assert last == 1.0


def test_settle_gives_the_others_time_to_show_peers():
    result, last = _probe({"a": 1.0, "b": 3.0}, settle_s=3.0)
    assert result == {"a": 1, "b": 1}
    # Stops as soon as every magnet has a peer, before the window ends.
    assert last == 3.0


def test_settle_ends_after_its_window_when_one_never_shows():
    result, last = _probe({"a": 1.0, "b": None}, settle_s=3.0)
    assert result == {"a": 1, "b": 0}
    assert 3.5 <= last <= 4.0


def test_settle_never_runs_past_the_timeout():
    result, last = _probe({"a": 9.0, "b": None}, timeout=10, settle_s=3.0)
    assert result == {"a": 1, "b": 0}
    assert last < 10.0
