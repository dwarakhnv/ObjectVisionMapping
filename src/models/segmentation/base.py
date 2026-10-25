"""Detector interface: any instance-segmentation (or detection) model plugs in here.

A detector takes a BGR frame (numpy, H x W x 3) and returns ``Detection``s in
that frame's pixel coordinates. The rest of the system only uses this
interface, so models can be swapped without touching the pipeline.
Weights live in ``data/models/<name>/``; code in ``src/models/<type>/<name>/``.
"""
from dataclasses import dataclass, field


@dataclass
class Detection:
    label: str                 # detector's own class name, e.g. "car"
    score: float               # 0-1
    box: tuple                 # (x1, y1, x2, y2) pixels
    polygon: list = field(default_factory=list)  # [(x, y), ...] outline in pixels; empty if the model has no masks
    class_key: str = ""        # filled in by the registry (car, bus, truck, person ...)


class Detector:
    name = "base"
    labels = ()                # every label the model can output

    def detect(self, frame, labels=None, min_score=0.35):
        """Detections in `frame`; restrict to `labels` (detector label names) when given."""
        raise NotImplementedError

    def info(self):
        return {"name": self.name, "labels": list(self.labels)}


_REGISTRY = {}


def register(name):
    def wrap(factory):
        _REGISTRY[name] = factory
        return factory
    return wrap


def available():
    return sorted(_REGISTRY)


def create(name, models_dir, **options):
    if name not in _REGISTRY:
        # Import built-in implementations lazily so their heavy dependencies load only when used.
        from .yolo import detector  # noqa: F401
    if name not in _REGISTRY:
        raise KeyError("Unknown detector '%s'. Available: %s" % (name, ", ".join(available())))
    return _REGISTRY[name](models_dir, **options)
