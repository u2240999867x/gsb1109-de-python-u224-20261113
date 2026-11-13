"""整筒浓度反演分层浓度：可辨识性、歧义解释与冲突定位。"""
from .model import Barrel, Layer
from .solver import Report, analyze, format_report, recovered_intervals

__all__ = [
    "Barrel",
    "Layer",
    "Report",
    "analyze",
    "format_report",
    "recovered_intervals",
    "layers_from_las",
]


def __getattr__(name):
    if name == "layers_from_las":
        from .lasio_io import layers_from_las

        return layers_from_las
    raise AttributeError(name)
