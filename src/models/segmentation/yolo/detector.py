"""Ultralytics YOLO instance segmentation (COCO classes).

Weights: data/models/<name>/<name>.pt. If the file is missing it is fetched
once by Ultralytics from its official release page and moved there.

Speed: on a machine with an Intel GPU (OpenVINO installed), the model is
exported once per frame shape to data/models/<name>/<name>_<h>x<w>_openvino_model
and run on the GPU, several times faster than PyTorch on the CPU and with the
same results. Anything that goes wrong there falls back to PyTorch on the CPU.
Note: Ultralytics YOLO is AGPL-3.0 licensed; check that suits your use.
"""
import math
import shutil
import threading
import traceback
from pathlib import Path

import numpy as np

from ..base import Detection, Detector, register

VARIANTS = ("yolo11n-seg", "yolo11s-seg", "yolo11m-seg", "yolo26n-seg", "yolo26s-seg", "yolo26m-seg")
STRIDE = 32


def intel_gpu():
    """True when OpenVINO is installed and sees a GPU."""
    try:
        import openvino
        return "GPU" in openvino.Core().available_devices
    except Exception:
        return False


def pick_device(device="auto"):
    """'intel:gpu' / 'intel:cpu' run through OpenVINO; 'cpu' / 'cuda:0' through PyTorch."""
    if device == "auto":
        return "intel:gpu" if intel_gpu() else "cpu"
    return device


class YoloSegDetector(Detector):
    def __init__(self, models_dir, variant="yolo11n-seg", device="auto", imgsz=640):
        from ultralytics import YOLO

        self.name = variant
        folder = Path(models_dir) / variant
        folder.mkdir(parents=True, exist_ok=True)
        weights = folder / (variant + ".pt")
        if not weights.exists():
            fetched = Path(YOLO(variant + ".pt").ckpt_path)
            shutil.move(str(fetched), weights)
        self.folder = folder
        self.model = YOLO(str(weights))
        self.weights = weights
        self.device = pick_device(device)
        self.imgsz = imgsz
        self.labels = tuple(self.model.names[i] for i in sorted(self.model.names))
        self._fast = {}                   # input shape (h, w) -> OpenVINO model
        self._lock = threading.Lock()     # one inference at a time per model

    @property
    def openvino(self):
        return self.device.startswith("intel:")

    def input_shape(self, h, w):
        """The network input for a frame: longest side imgsz, the other scaled and padded to a multiple of 32."""
        r = self.imgsz / max(h, w)
        return (int(math.ceil(round(h * r) / STRIDE) * STRIDE), int(math.ceil(round(w * r) / STRIDE) * STRIDE))

    def _fast_model(self, shape):
        """The OpenVINO model for this input shape, exported on first use (takes ~30 s once)."""
        if shape not in self._fast:
            from ultralytics import YOLO

            path = self.folder / ("%s_%dx%d_openvino_model" % (self.name, shape[0], shape[1]))
            if not (path / "metadata.yaml").exists():
                exported = YOLO(str(self.weights)).export(format="openvino", imgsz=shape, verbose=False)
                shutil.rmtree(path, ignore_errors=True)
                shutil.move(exported, path)
            self._fast[shape] = YOLO(str(path), task="segment")
        return self._fast[shape]

    def _predict(self, frame, **kw):
        if self.openvino:
            shape = self.input_shape(*frame.shape[:2])
            try:
                return self._fast_model(shape).predict(frame, imgsz=shape, device=self.device, **kw)[0]
            except Exception:  # no GPU driver, export failed ...: carry on, slower
                traceback.print_exc()
                print("OpenVINO unavailable; running %s with PyTorch on the CPU" % self.name)
                self.device = "cpu"
        return self.model.predict(frame, imgsz=self.imgsz, device=self.device, **kw)[0]

    def detect(self, frame, labels=None, min_score=0.35):
        classes = None
        if labels:
            wanted = {l.lower() for l in labels}
            classes = [i for i, n in self.model.names.items() if n.lower() in wanted]
            if not classes:
                return []
        with self._lock:
            result = self._predict(frame, conf=min_score, classes=classes, verbose=False, retina_masks=False)
        out = []
        if result.boxes is None:
            return out
        boxes = result.boxes.xyxy.cpu().numpy()
        scores = result.boxes.conf.cpu().numpy()
        cls = result.boxes.cls.cpu().numpy().astype(int)
        polys = result.masks.xy if result.masks is not None else [None] * len(boxes)
        for b, s, c, poly in zip(boxes, scores, cls, polys):
            polygon = []
            if poly is not None and len(poly):
                pts = np.asarray(poly, dtype=float)
                step = max(1, len(pts) // 60)  # keep outlines light for the browser
                polygon = [(round(float(x), 1), round(float(y), 1)) for x, y in pts[::step]]
            out.append(Detection(label=self.model.names[int(c)], score=float(s),
                                 box=tuple(float(v) for v in b), polygon=polygon))
        return out

    @property
    def runs_on(self):
        return {"intel:gpu": "Intel GPU (OpenVINO)", "intel:cpu": "CPU (OpenVINO)"}.get(self.device, self.device.upper() + " (PyTorch)")

    def info(self):
        return {"name": self.name, "labels": list(self.labels), "weights": str(self.weights), "device": self.device,
                "runs_on": self.runs_on, "imgsz": self.imgsz, "license": "AGPL-3.0 (Ultralytics)"}


for _variant in VARIANTS:
    register(_variant)(lambda models_dir, v=_variant, **opts: YoloSegDetector(models_dir, v, **opts))
