"""U2: download state file persists speed/peers/eta alongside progress."""

import json
from dataclasses import dataclass

from auto_torrent.download import (
    _estimate_eta,
    _load_resume_data,
    _resume_path,
    _update_state_progress,
)


@dataclass
class FakeStatus:
    download_rate: int
    num_peers: int
    total_wanted: int
    total_wanted_done: int


def _write_initial(path):
    path.write_text(json.dumps({"id": "abc", "progress": 0.0, "status": "downloading"}))


def test_persists_speed_peers_eta(tmp_path):
    state = tmp_path / "abc.json"
    _write_initial(state)

    _update_state_progress(
        state, 0.34, speed_bytes_per_s=2_500_000, peers=8, eta_s=120
    )

    data = json.loads(state.read_text())
    assert data["progress"] == 0.34
    assert data["speed_bytes_per_s"] == 2_500_000
    assert data["peers"] == 8
    assert data["eta_s"] == 120
    # untouched fields survive
    assert data["id"] == "abc"


def test_eta_none_on_zero_rate():
    status = FakeStatus(download_rate=0, num_peers=2, total_wanted=1000, total_wanted_done=100)
    assert _estimate_eta(status) is None


def test_eta_zero_when_complete():
    status = FakeStatus(download_rate=500, num_peers=2, total_wanted=1000, total_wanted_done=1000)
    assert _estimate_eta(status) == 0


def test_eta_computed_from_remaining_over_rate():
    status = FakeStatus(
        download_rate=1000, num_peers=5, total_wanted=10_000, total_wanted_done=2_000
    )
    # (10000 - 2000) / 1000 = 8s
    assert _estimate_eta(status) == 8


def test_status_only_update_keeps_prior_metrics(tmp_path):
    state = tmp_path / "abc.json"
    _write_initial(state)
    _update_state_progress(state, 0.5, speed_bytes_per_s=1000, peers=3, eta_s=10)

    # A later call that only flips status must not clobber the metrics.
    _update_state_progress(state, 0.5, status="completed")

    data = json.loads(state.read_text())
    assert data["status"] == "completed"
    assert data["speed_bytes_per_s"] == 1000
    assert data["peers"] == 3


def test_legacy_reader_unaffected(tmp_path):
    state = tmp_path / "abc.json"
    _write_initial(state)
    _update_state_progress(state, 0.7, speed_bytes_per_s=999, peers=1, eta_s=5)
    data = json.loads(state.read_text())
    # A reader that only knows 'progress' still works.
    assert data.get("progress") == 0.7


# --- U4 resume helpers ---------------------------------------------------


def test_resume_path_beside_state_file(tmp_path):
    state = tmp_path / "abc123.json"
    assert _resume_path(state) == tmp_path / "abc123.resume"


def test_resume_path_none_without_state_file():
    assert _resume_path(None) is None


def test_load_resume_data_reads_blob(tmp_path):
    blob = tmp_path / "abc123.resume"
    blob.write_bytes(b"resume-bytes")
    assert _load_resume_data(blob) == b"resume-bytes"


def test_load_resume_data_missing_returns_none(tmp_path):
    assert _load_resume_data(tmp_path / "nope.resume") is None
    assert _load_resume_data(None) is None
