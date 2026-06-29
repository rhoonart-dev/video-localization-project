"""engine/mask.py — 기하/temporal smoothing 순수 로직."""
from engine.mask import dilate_bbox, iou, merge_boxes, smooth_temporal


def test_iou_identical():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0


def test_iou_disjoint():
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0


def test_iou_partial():
    # inter=5*10=50, union=100+100-50=150
    assert abs(iou((0, 0, 10, 10), (5, 0, 15, 10)) - (50 / 150)) < 1e-9


def test_dilate_bbox_clamps_to_frame():
    assert dilate_bbox((5, 5, 10, 10), 10, 100, 100) == (0, 0, 20, 20)


def test_merge_boxes_combines_overlap_keeps_distant():
    out = merge_boxes([(0, 0, 10, 10), (1, 1, 11, 11), (50, 50, 60, 60)], iou_thresh=0.5)
    assert len(out) == 2


def test_smooth_union_fills_gap():
    frames = [(0, [(0, 0, 10, 10)]), (15, []), (30, [(0, 0, 10, 10)])]
    out = smooth_temporal(frames, window=3, strategy="union")
    assert out[15], "union 은 이웃 박스로 빈 프레임을 메워야 한다"


def test_smooth_vote_filters_singleton():
    frames = [(0, [(0, 0, 10, 10)]), (1, []), (2, [])]
    out = smooth_temporal(frames, window=3, strategy="vote", vote_ratio=0.6)
    assert out[0] == [], "vote 는 소수 등장 박스를 제거해야 한다"
