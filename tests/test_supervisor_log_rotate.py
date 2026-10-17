import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "deployment" / "supervisor_log_rotate.py"
_spec = importlib.util.spec_from_file_location("supervisor_log_rotate", _PATH)
lr = importlib.util.module_from_spec(_spec)
sys.modules["supervisor_log_rotate"] = lr
_spec.loader.exec_module(lr)


def test_small_files_are_left_alone(tmp_path):
    (tmp_path / "gateway.log").write_bytes(b"x" * 10)
    assert lr.rotate_dir(tmp_path, max_bytes=100, backups=3) == []
    assert not (tmp_path / "gateway.log.1").exists()


def test_rotates_by_copy_and_truncate_keeping_the_open_file(tmp_path):
    log = tmp_path / "gateway.log"
    log.write_bytes(b"old" * 50)
    with open(log, "ab") as writer:  # a service's >> handle
        assert lr.rotate_dir(tmp_path, max_bytes=100, backups=3) == ["gateway.log"]
        writer.write(b"new")
    assert (tmp_path / "gateway.log.1").read_bytes() == b"old" * 50
    assert log.read_bytes() == b"new"


def test_keeps_only_the_configured_backups(tmp_path):
    log = tmp_path / "gateway.log"
    for i in range(5):
        log.write_bytes(bytes([65 + i]) * 200)
        lr.rotate_dir(tmp_path, max_bytes=100, backups=3)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["gateway.log", "gateway.log.1", "gateway.log.2", "gateway.log.3"]
    assert (tmp_path / "gateway.log.1").read_bytes()[:1] == b"E"
    assert (tmp_path / "gateway.log.3").read_bytes()[:1] == b"C"


def test_skips_logs_supervisord_rotates_itself(tmp_path):
    for name in ("supervisord.log", "crash-watch.log", "gateway.supervisor.log"):
        (tmp_path / name).write_bytes(b"x" * 200)
    assert lr.rotate_dir(tmp_path, max_bytes=100, backups=3) == []
