"""Object classes the system knows about, with average real-world sizes.

The registry is generic: each class names the detector labels that map to it
and gives average dimensions (metres) used to estimate distance and size, plus
optional size-based subtypes (e.g. adult / child). Classes are loaded from
``data/object_classes.json`` when it exists; adding an object type is a JSON
edit (and, if the detector doesn't know it yet, a model that does).
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CLASSES = [
    {
        "key": "car", "label": "Car", "detector_labels": ["car"], "group": "vehicle",
        "dimensions": {"length": 4.5, "width": 1.8, "height": 1.5},
        "moving": True,
        "subtypes": [],
    },
    {
        "key": "bus", "label": "Bus", "detector_labels": ["bus"], "group": "vehicle",
        "dimensions": {"length": 12.0, "width": 2.55, "height": 3.2},
        "moving": True,
        "subtypes": [],
    },
    {
        "key": "truck", "label": "Truck", "detector_labels": ["truck"], "group": "vehicle",
        "dimensions": {"length": 8.0, "width": 2.5, "height": 3.5},
        "moving": True,
        "subtypes": [
            {"key": "pickup", "label": "Pickup / van", "max_height": 2.4},
            {"key": "truck", "label": "Truck", "min_height": 2.4},
        ],
    },
    {
        "key": "person", "label": "Person", "detector_labels": ["person"], "group": "person",
        "dimensions": {"length": 0.5, "width": 0.5, "height": 1.7},
        "moving": True,
        "subtypes": [
            {"key": "child", "label": "Child", "max_height": 1.4, "dimensions": {"length": 0.4, "width": 0.4, "height": 1.15}},
            {"key": "adult", "label": "Adult", "min_height": 1.4},
        ],
    },
]


@dataclass
class Subtype:
    key: str
    label: str
    min_height: float = 0.0
    max_height: float = float("inf")
    dimensions: dict = None

    def matches(self, height):
        return self.min_height <= height < self.max_height


@dataclass
class ObjectClass:
    key: str
    label: str
    detector_labels: list
    dimensions: dict          # average {length, width, height} in metres
    moving: bool = True
    subtypes: list = field(default_factory=list)
    group: str = ""           # classes in one group can be confused by a detector (car/truck)

    @property
    def height(self):
        return self.dimensions["height"]

    def subtype_for(self, height):
        """The subtype that matches an estimated height (metres), or None."""
        if height is None:
            return None
        for s in self.subtypes:
            if s.matches(height):
                return s
        return None

    def to_dict(self):
        return {"key": self.key, "label": self.label, "detector_labels": self.detector_labels,
                "dimensions": self.dimensions, "moving": self.moving, "group": self.group,
                "subtypes": [{"key": s.key, "label": s.label, "min_height": s.min_height,
                              "max_height": None if s.max_height == float("inf") else s.max_height,
                              "dimensions": s.dimensions} for s in self.subtypes]}


class ObjectRegistry:
    def __init__(self, classes):
        self.classes = {c.key: c for c in classes}
        self._by_label = {}
        for c in classes:
            for lab in c.detector_labels:
                self._by_label[lab.lower()] = c

    @classmethod
    def from_list(cls, items):
        classes = []
        for d in items:
            dims = d.get("dimensions") or {}
            subtypes = [Subtype(key=s["key"], label=s.get("label", s["key"]), min_height=float(s.get("min_height") or 0),
                                max_height=float(s["max_height"]) if s.get("max_height") is not None else float("inf"),
                                dimensions=s.get("dimensions")) for s in d.get("subtypes") or []]
            classes.append(ObjectClass(key=d["key"], label=d.get("label", d["key"]),
                                       detector_labels=list(d.get("detector_labels") or [d["key"]]),
                                       dimensions={"length": float(dims.get("length", 1)), "width": float(dims.get("width", 1)),
                                                   "height": float(dims.get("height", 1))},
                                       moving=bool(d.get("moving", True)), subtypes=subtypes,
                                       group=str(d.get("group") or d["key"])))
        return cls(classes)

    @classmethod
    def load(cls, path=None):
        if path and Path(path).exists():
            with open(path, encoding="utf-8") as fh:
                return cls.from_list(json.load(fh)["classes"])
        return cls.from_list(DEFAULT_CLASSES)

    @staticmethod
    def write_default(path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"classes": DEFAULT_CLASSES}, fh, indent=2)

    def for_label(self, detector_label):
        """The class a detector label maps to, or None if we don't track it."""
        return self._by_label.get(str(detector_label).lower())

    def detector_labels(self):
        return sorted(self._by_label)

    def groups(self):
        return {k: c.group for k, c in self.classes.items()}

    def to_list(self):
        return [c.to_dict() for c in self.classes.values()]
