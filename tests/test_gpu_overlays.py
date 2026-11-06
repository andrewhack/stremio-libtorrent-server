"""The GPU compose overlays hand the container only what each GPU needs.

The NVIDIA overlay mapped `/dev/dri` too, so on an NVIDIA-only host, which has no DRM directory,
the container could not be created. And mapping the whole directory, cards included, is refused in
some unprivileged containers, while VAAPI needs only a render node. So NVENC and VAAPI are now two
overlays, applied one or both on top of compose.yaml, and the VAAPI one maps one render node.
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]


def _service(name):
    doc = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    return doc["services"]["stremio-libtorrent-server"]


def test_the_nvidia_overlay_needs_no_drm_node():
    svc = _service("compose.gpu.yaml")
    assert svc.get("runtime") == "nvidia"
    assert not any("/dev/dri" in str(d) for d in svc.get("devices") or [])


def test_the_vaapi_overlay_maps_one_render_node_not_the_directory():
    svc = _service("compose.vaapi.yaml")
    devices = svc["devices"]
    assert len(devices) == 1
    assert "renderD128" in devices[0] and "VAAPI_DEVICE" in devices[0]
    assert "/dev/dri:/dev/dri" not in devices[0]
    assert "runtime" not in svc, "VAAPI must not need the NVIDIA runtime"
