"""engine/inpaint.py — 백엔드 디스패치 + 라이선스 가드."""
from engine.inpaint import OpenCVBackend, make_inpainter, select_backend

CFG = {
    "inpaint": {"default_backend": "opencv",
                "mode_by_content": {"mukbang": "sttn", "anime": "lama", "default": "lama"}},
    "paths": {"weights_dir": "weights"},
}


def test_select_backend_by_content():
    assert select_backend("mukbang", CFG) == "sttn"
    assert select_backend("anime", CFG) == "lama"


def test_select_backend_default():
    assert select_backend(None, CFG) == "lama"
    assert select_backend("unknown_type", CFG) == "lama"


def test_make_inpainter_unknown_raises():
    raised = False
    try:
        make_inpainter("nope", CFG)
    except ValueError:
        raised = True
    assert raised


def test_make_inpainter_opencv_constructs():
    assert isinstance(make_inpainter("opencv", CFG), OpenCVBackend)


def test_propainter_license_gate_blocks():
    raised = False
    try:
        make_inpainter("propainter", CFG)
    except RuntimeError as e:
        raised = "ProPainter" in str(e) or "라이선스" in str(e)
    assert raised, "ack 없이는 propainter 차단되어야 한다"


def test_propainter_acked_passes_gate_then_not_implemented():
    cfg = {"paths": {"weights_dir": "weights"},
           "inpaint": {**CFG["inpaint"], "propainter_commercial_ack": True}}
    raised = False
    try:
        make_inpainter("propainter", cfg)
    except NotImplementedError:
        raised = True  # 가드는 통과, 어댑터는 미연동
    assert raised
