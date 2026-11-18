"""glm-flash 的分段输出容错：JSON 里夹说明/连吐两个对象，以及漏写 t0。"""
from backend.services.bilibili.visual.llm import parse_json_loose
from backend.services.bilibili.visual.seg_filter import normalize_sections, windows_from_sections


def test_parse_json_loose_two_objects():
    # 模型连吐两个 JSON：贪婪匹配会拼成 "{...}\n{...}" → json.loads 报 Extra data
    assert parse_json_loose('{"genre":"技术拆解"}\n\n{"genre":"课程讲解"}') == {
        "genre": "技术拆解"}


def test_parse_json_loose_prose_around_json():
    resp = '好的，结果如下：\n{"sections":[{"t0":0,"t1":60,"score":3}]}\n以上。'
    assert parse_json_loose(resp) == {"sections": [{"t0": 0, "t1": 60, "score": 3}]}


def test_parse_json_loose_takes_array():
    assert parse_json_loose('结果：[1, 2]\n[3]') == [1, 2]


def test_parse_json_loose_no_json():
    assert parse_json_loose("这里没有 JSON") is None
    assert parse_json_loose("") is None


def test_normalize_fills_missing_t0():
    secs = [{"t0": 0, "t1": 60, "score": 1},
            {"t1": 125, "score": 3},          # glm-flash 实测就长这样
            {"t1": 170, "score": 1}]
    out = normalize_sections(secs, 200)
    assert [s["t0"] for s in out] == [0.0, 60.0, 125.0]
    assert [s["t1"] for s in out] == [60.0, 125.0, 170.0]


def test_normalize_clamps_and_drops_invalid():
    secs = [{"t1": 500}, {"t1": "abc"}, {"t0": 10}]
    out = normalize_sections(secs, 100)
    assert len(out) == 1          # 第二条 t1 不是数字、第三条没有 t1
    assert out[0]["t1"] == 100.0  # 超过片长要夹住


def test_windows_from_sections_after_normalize():
    secs = normalize_sections([{"t0": 0, "t1": 60, "score": 3},
                               {"t1": 120, "score": 3}], 130)
    wins = windows_from_sections(secs, 3, 130)
    assert len(wins) == 1          # 补了 t0 后两段首尾相接，合并成一窗
    assert wins[0]["t0"] == 0.0
