import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH))

import fixtures as F  # noqa: E402
from server import BenchServer  # noqa: E402


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    F.enc_h264(d / "tiny.mkv", 640, 360, 6 / 60, 2, 24, 5, False)  # 6 s, GOP 1 s, with barcode
    (d / "blob.bin").write_bytes(bytes(range(256)) * 4096)        # 1 MiB of known bytes
    (d / "notvideo.mkv").write_bytes(b"this is not a video" * 1000)
    return d


@pytest.fixture()
def srv(media):
    s = BenchServer(media).start()
    yield s
    s.stop()
