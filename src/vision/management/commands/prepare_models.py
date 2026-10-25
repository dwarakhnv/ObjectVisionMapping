"""Download the detection model(s) and prepare them for this machine's GPU, so cameras start at once.

    python manage.py prepare_models                                 # the model OVM will use, for 720p frames
    python manage.py prepare_models yolo26s-seg yolo11n-seg --size 1920x1080 --size 1280x720
    python manage.py prepare_models --device intel:gpu.1            # a specific GPU

Weights go to data/models/<name>/<name>.pt. On an Intel GPU each model is also
exported to OpenVINO once per frame shape (data/models/<name>/<name>_<h>x<w>_openvino_model,
about 30 s each). Without this command both happen automatically the first
time a camera is detected; this just does it ahead of time and shows the speed.
"""
import time

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from models.segmentation.yolo.detector import VARIANTS, YoloSegDetector, cuda_gpus, default_variant, intel_gpus, pick_device


def _size(text):
    try:
        w, h = (int(v) for v in text.lower().split("x"))
        return w, h
    except ValueError:
        raise CommandError("Sizes look like 1280x720 (width x height), not '%s'" % text)


class Command(BaseCommand):
    help = "Download the detection model(s) and prepare them for this machine's GPU."

    def add_arguments(self, parser):
        parser.add_argument("names", nargs="*", help="Models to prepare (default: the one OVM uses). Choices: " + ", ".join(VARIANTS))
        parser.add_argument("--size", action="append", type=_size, help="Camera frame size, e.g. 1280x720 (repeatable; default 1280x720)")
        parser.add_argument("--device", default=settings.OVM_DEVICE, help="auto, cuda:0, intel:gpu, intel:gpu.1, cpu ... (default: OVM_DEVICE)")

    def handle(self, *args, **opts):
        gpus = cuda_gpus() + intel_gpus()
        self.stdout.write("GPUs found: " + (", ".join(gpus) if gpus else "none (detection runs on the CPU)"))
        device = pick_device(opts["device"])
        names = opts["names"] or [settings.OVM_DETECTOR or default_variant(device)]
        unknown = [n for n in names if n not in VARIANTS]
        if unknown:
            raise CommandError("Unknown model %s. Choices: %s" % (", ".join(unknown), ", ".join(VARIANTS)))
        for name in names:
            self.stdout.write("\n%s on %s" % (name, device))
            det = YoloSegDetector(settings.MODELS_DIR, name, device=device, imgsz=settings.OVM_IMGSZ)
            self.stdout.write("  weights: %s" % det.weights)
            for w, h in opts["size"] or [(1280, 720)]:
                frame = np.zeros((h, w, 3), np.uint8)
                started = time.monotonic()
                det.detect(frame)  # downloads / exports on first use
                ready = time.monotonic() - started
                started = time.monotonic()
                for _ in range(5):
                    det.detect(frame)
                ms = (time.monotonic() - started) / 5 * 1000
                self.stdout.write("  %dx%d: ready in %.0f s, then %.0f ms a frame on %s" % (w, h, ready, ms, det.runs_on))
        self.stdout.write(self.style.SUCCESS("\nDone. Start the server and open a camera on the Detect page."))
