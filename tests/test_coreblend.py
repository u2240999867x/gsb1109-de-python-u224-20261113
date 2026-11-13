"""coreblend 的单元测试。"""
import json

import numpy as np
import pytest

from coreblend import Barrel, Layer, analyze, format_report, recovered_intervals
from coreblend.cli import main as cli_main
from coreblend.lasio_io import layers_from_las


def test_aligned_barrels_determined():
    layers = [Layer("L1", 0, 5), Layer("L2", 5, 10)]
    barrels = [
        Barrel("B1", 0, 5, concentration=10.0),
        Barrel("B2", 5, 10, concentration=20.0),
    ]
    report = analyze(layers, barrels)
    assert report.consistent
    values = {r.name: r for r in report.layers}
    assert values["L1"].status == "determined"
    assert values["L2"].status == "determined"
    assert values["L1"].value == pytest.approx(10.0)
    assert values["L2"].value == pytest.approx(20.0)


def test_misaligned_barrels_not_just_copying_barrel_mean():
    # 真实分层浓度 10 / 20；B1 跨层，整筒均值 80/6 != 任何一层
    layers = [Layer("L1", 0, 4), Layer("L2", 4, 8)]
    barrels = [
        Barrel("B1", 0, 6, concentration=(4 * 10 + 2 * 20) / 6),
        Barrel("B2", 6, 8, concentration=20.0),
    ]
    report = analyze(layers, barrels)
    assert report.consistent
    values = {r.name: r for r in report.layers}
    assert values["L1"].status == "determined"
    assert values["L1"].value == pytest.approx(10.0)
    assert values["L1"].value != pytest.approx(barrels[0].concentration)
    assert values["L2"].value == pytest.approx(20.0)


def test_proportional_layers_give_two_equivalent_explanations():
    # L1、L2 在每筒中都按 1:1 出现，整筒均值只能约束二者平均
    layers = [Layer("L1", 0, 5), Layer("L2", 5, 10)]
    barrels = [
        Barrel("B1", 0, 10, concentration=15.0),
        Barrel("B2", 2, 8, concentration=15.0),
    ]
    report = analyze(layers, barrels)
    assert report.consistent
    values = {r.name: r for r in report.layers}
    assert values["L1"].status == "undetermined"
    assert values["L2"].status == "undetermined"
    assert len(report.groups) == 1
    group = report.groups[0]
    assert set(group.layers) == {"L1", "L2"}
    a, b = group.explanation_a, group.explanation_b
    assert a != b  # 两种不同的解释
    for expl in (a, b):
        assert all(v >= 0 for v in expl.values())
        # 两种解释都复现每筒化验值
        for br in barrels:
            assert (expl["L1"] + expl["L2"]) / 2 == pytest.approx(br.concentration)


def test_inconsistent_batch_points_to_conflicting_barrel():
    layers = [Layer("L1", 0, 5)]
    barrels = [
        Barrel("B1", 0, 5, concentration=10.0, error=0.1),
        Barrel("B2", 0, 5, concentration=10.2, error=0.1),
        Barrel("B3", 0, 5, concentration=30.0, error=0.5),
    ]
    report = analyze(layers, barrels)
    assert not report.consistent
    assert report.suspect_barrels == ["B3"]
    assert report.residuals["B3"] > report.residuals["B1"]


def test_lost_interval_excludes_unrecovered_layer():
    layers = [Layer("L1", 0, 5), Layer("L2", 5, 10)]
    barrels = [
        Barrel("B1", 0, 10, concentration=7.0, lost_intervals=[(5.0, 10.0)]),
        Barrel("B2", 5, 10, concentration=20.0),
    ]
    report = analyze(layers, barrels)
    values = {r.name: r for r in report.layers}
    assert values["L1"].status == "determined"
    assert values["L1"].value == pytest.approx(7.0)
    assert values["L2"].value == pytest.approx(20.0)


def test_recovered_length_without_lost_intervals_assumes_top_recovery():
    warnings: list[str] = []
    barrel = Barrel("B1", 0, 10, concentration=5.0, recovered_length=4.0)
    intervals = recovered_intervals(barrel, warnings)
    assert intervals == [(0, 4.0)]
    assert any("筒顶" in w for w in warnings)


def test_unsampled_layer_is_flagged():
    layers = [Layer("L1", 0, 5), Layer("L2", 20, 25)]
    barrels = [Barrel("B1", 0, 5, concentration=3.0)]
    report = analyze(layers, barrels)
    values = {r.name: r for r in report.layers}
    assert values["L2"].status == "unsampled"


def test_error_tolerance_allows_noisy_assays():
    layers = [Layer("L1", 0, 5)]
    barrels = [
        Barrel("B1", 0, 5, concentration=10.0, error=0.5),
        Barrel("B2", 0, 5, concentration=10.4, error=0.5),
    ]
    report = analyze(layers, barrels)
    assert report.consistent


def test_layers_from_las(tmp_path):
    las_text = """~Version Information
VERS. 2.0 : LAS version
~Well Information
STRT.M 1000.0 : start
STOP.M 1006.0 : stop
STEP.M 1.0 : step
NULL. -999.25 : null
~Curve Information
DEPT.M : depth
ZONE. : zone code
~A
1000.0 1
1001.0 1
1002.0 2
1003.0 2
1004.0 2
1005.0 -999.25
"""
    path = tmp_path / "well.las"
    path.write_text(las_text)
    layers = layers_from_las(str(path))
    assert [(ly.name, ly.top, ly.base) for ly in layers] == [
        ("ZONE=1", 1000.0, 1002.0),
        ("ZONE=2", 1002.0, 1005.0),
    ]


def test_cli_json_roundtrip(tmp_path, capsys):
    spec = {
        "layers": [{"name": "L1", "top": 0, "base": 5}, {"name": "L2", "top": 5, "base": 10}],
        "barrels": [
            {"name": "B1", "top": 0, "base": 10, "concentration": 15.0, "error": 0.2},
            {"name": "B2", "top": 2, "base": 8, "concentration": 15.0, "error": 0.2},
        ],
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    assert cli_main([str(spec_path)]) == 0
    out = capsys.readouterr().out
    assert "歧义组" in out
    assert "解释一" in out and "解释二" in out

    assert cli_main([str(spec_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["consistent"] is True
    assert len(report["groups"]) == 1


def test_format_report_mentions_conflict():
    layers = [Layer("L1", 0, 5)]
    barrels = [
        Barrel("B1", 0, 5, concentration=10.0, error=0.1),
        Barrel("B2", 0, 5, concentration=50.0, error=0.1),
    ]
    text = format_report(analyze(layers, barrels))
    assert "不相容" in text
    assert "冲突岩芯" in text
