# Technical reference

## Layout

```
ObjectVisionMapping/
  src/                         Django project (run manage.py from here)
    ovm/                       settings, URLs
    environments/              Phase 1: Environment class, geometry, 3D explorer, API
      environment.py           Environment, CoordinateSystem, ReferencePoint, Camera, CameraSource, Calibration
      geometry.py              camera poses, projection, ground intersection, pose solving
    vision/                    Phase 2: Live page, feed workers, sources, API
    mapping/                   Phase 3: calibration, live map, multi-camera fusion
      management/commands/     make_demo, eval_demo
    models/
      objects/                 object class registry (sizes) and per-object estimates
      segmentation/            detector interface + yolo/ implementation
      tracking/sort/           multi-object tracker
    static/ovm/scene3d.js      shared Three.js environment view
    static/vendor/three/       Three.js r170 (MIT), served locally
    tests/                     python manage.py test tests
  data/
    environments/<name>/environment.json   (+ calibration/<camera>.jpg uploads)
    models/<model>/<weights>               detector weights
    sources/                               uploaded videos / images, demo clips
    object_classes.json                    object types and average sizes
  config/secrets.json          keys and camera credentials (not committed)
  docs/
```

## Conventions

**World frame.** Every environment is converted to a local
**east-north-up** frame in **metres** (x east, y north, z up). Geo
environments use exact WGS84 ↔ ECEF ↔ ENU conversion about their origin.
Custom-unit environments scale by `meters_per_unit`. The **ground** is the
plane z = `ground` (in the environment's units, or altitude for geo).
Detected objects are placed on it.

**Camera orientation** is described the way an installer would:

| Field | Meaning |
|---|---|
| `heading` | Compass direction of the optical axis, degrees clockwise from north (0–360) |
| `tilt` | Degrees below the horizon (positive = looking down) |
| `roll` | Image rotation, degrees clockwise as seen from behind the camera |
| `hfov` | Horizontal field of view, degrees |
| `resolution` | Native `[width, height]`; the vertical field of view follows (square pixels) |
| `position` + `height` | Ground point under the camera, and mount height above it |

The camera model is an ideal pinhole with no lens distortion. Strongly
distorting lenses (fisheyes) are not yet modelled.

## environment.json

```json
{
  "schema": 1,
  "name": "Demo plaza", "slug": "demo_plaza", "description": "...", "extent": 60,
  "coordinates": {"kind": "local", "units": "m", "meters_per_unit": 1.0, "ground": 0},
  "reference_points": [
    {"id": "P1", "name": "Marker NW near", "position": {"x": -8, "y": 10, "z": 0}, "description": ""}
  ],
  "cameras": [{
    "id": "north", "name": "Plaza north-facing",
    "position": {"x": 0.0, "y": -2.0, "z": 0}, "height": 6.0,
    "heading": 0.0, "tilt": 14.04, "roll": 0.0, "hfov": 70.0, "resolution": [1280, 720],
    "source": {"kind": "file", "uri": "demo_plaza_north.mp4", "secret": "", "loop": true},
    "calibration": {
      "resolution": [1280, 720],
      "points": [{"point_id": "P1", "u": 0.1531, "v": 0.8712}],
      "solved": {"rmse_px": 0.33, "points": 8, "fov_estimated": false, "at": 1792190000.0}
    }
  }],
  "created": 1792190000.0, "updated": 1792190000.0
}
```

Geo coordinates use `{"kind": "geo", "origin": {"lat", "lon", "alt"}, "ground": <alt>}`
and positions `{"lat", "lon", "alt"}`. Calibration pixels (`u`, `v`) are
fractions of the image size, which is what makes them hold across
resolutions.

## Pipeline (per feed)

```
source frame ─► detector (instance segmentation) ─► registry maps labels to classes
            ─► tracker, if the environment turns it on (Kalman + Hungarian, two-pass) ─► stable ids
            ─► estimator (size, distance, direction, world position, speed, heading)
            ─► latest result + last few frames ─► browser / fusion
```

The camera's own reader thread delivers frames at full rate; the detection
worker always takes the newest one, so it never falls behind. The Detect page
plays the camera's live video (MJPEG, `/api/feeds/<feed>/video`) and draws the
latest detections over it, moved ahead along each object's motion; frames are
JPEG-encoded only when a page asks for one.

- **Detector:** `models/segmentation/base.py` defines `Detector.detect(frame)
  -> [Detection(label, score, box, polygon)]` (Ultralytics YOLO, COCO classes,
  inference size 960 px).
  - **With an Intel GPU** and OpenVINO installed, the default is `yolo26s-seg`
    on the GPU: about 95 ms per 720p frame (i7-1165G7 / Iris Xe), 8-9 results
    a second. The model is exported once per frame shape to
    `data/models/<name>/<name>_<h>x<w>_openvino_model` (about 30 s, the first
    time) and gives the same detections as PyTorch.
  - **With an NVIDIA GPU** (CUDA build of PyTorch) it is `yolo26s-seg` on that
    GPU; `OVM_DEVICE=auto` prefers NVIDIA, then Intel, then the CPU.
  - **Otherwise** it is `yolo11n-seg` with PyTorch on the CPU, about 200 ms per
    frame.
  - `python manage.py prepare_models` downloads and exports models ahead of
    time and prints the devices found and the speed.
  - `OVM_DETECTOR` picks another model (`yolo11s-seg`, `yolo26m-seg` ...),
    `OVM_DEVICE` the hardware (`auto`, `intel:gpu`, `intel:gpu.1`, `intel:cpu`, `cpu`,
    `cuda:0`, `cuda:1`), `OVM_IMGSZ` the inference size, and `OVM_MAX_FPS` (15) caps
    the results per second.
- **Confidence:** detections below 0.3 are dropped (0.25 for people, who are
  the priority and often score low when far away or partly hidden).
- **Tracking is optional, per environment** (`"tracking"` in its file, the
  "Track objects" switch on the Environments page, off by default). Off: each
  result lists every detection in that frame, ids number them, and there is
  no speed, motion heading or prediction. On:
- **Tracker:** a constant-velocity Kalman filter on box centre, area and
  aspect ratio, with IoU matching. Every detection not matched to a track
  starts a new one and is shown at once, so faint or fast objects are never
  held back.
  - Confident detections are matched first, then weak ones get a second
    chance against the tracks left unmatched.
  - Tracks only take detections from their own **class group**. Car, bus
    and truck share the "vehicle" group, because detectors flip between
    them.
  - A track survives 15 missed frames.
- **Estimator** (`models/objects/estimate.py`):
  - **Calibrated camera:** the box's bottom-centre ray meets the ground. The
    object's centre is half its depth beyond that point. The top-of-box ray
    measures its height (median over the track), which picks the subtype.
    World velocity, fitted over the last second, gives speed and compass
    heading. Its components along and across the camera's line of sight
    give the direction.
  - **Uncalibrated:** distance = focal × average height ÷ box height.
    Direction compares box growth rate (`d ln h / dt`) with sideways angular
    rate (`dx / focal / dt`). Both are in 1/s, so they compare directly.
  - Objects cut off by the top or bottom of the frame get no
    height-based distance or size.
- **Feed workers** (`vision/feeds.py`): one thread per active feed.
  - Video files play in real time and loop (frames are skipped when
    processing is slower).
  - Workers reload the environment when its file changes, so a new
    calibration applies within a second.
  - A worker stops 30 s after its last request (`OVM_FEED_IDLE_SECONDS`).
- **Fusion** (`mapping/fusion.py`): objects from different calibrated
  cameras are the same object when:
  - they're in the same class group,
  - they come from different cameras, and
  - they're within a radius of 1.5 m + half the object's size + 3 % of its
    distance from the camera.

  The merged position is weighted towards nearer cameras. Global IDs carry
  over while any member track continues.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET / POST | `/api/environments` | List; create (body: environment JSON) |
| GET / PUT / DELETE | `/api/environments/<slug>` | Read (with world-space view), replace, delete |
| POST | `/api/environments/preview` | Validate and return the world view without saving |
| POST | `/api/environments/convert` | `{coordinates, world:[x,y,z]}` → position, or `{coordinates, position}` → world |
| GET | `/api/feeds` | Feeds: environment cameras (`<slug>.<camera>`) and files (`~<name>`) |
| GET | `/api/feeds/<id>/latest?after=<frame>` | Latest analysis; waits up to 1.5 s for a newer frame |
| GET | `/api/feeds/<id>/frame/<frame>.jpg` | The exact frame an analysis belongs to |
| GET | `/api/feeds/<id>/snapshot.jpg` | Newest frame (starts the feed if needed) |
| POST | `/api/feeds/<id>/stop` | Stop a feed |
| GET / POST | `/api/sources` | List / upload files in `data/sources` |
| GET | `/api/object-classes`, `/api/detector` | Registry and model info |
| GET | `/api/mapping/<slug>/cameras/<cam>` | Camera + projected overlay (points, ground grid) |
| PUT | `.../points` | Save pixel ↔ point pairs |
| POST | `.../solve` | Solve pose (`estimate_fov` optional) and save |
| POST | `.../reset` | Mark uncalibrated |
| GET / POST / DELETE | `.../image` | Calibration still image |
| GET | `/api/mapping/<slug>/objects` | Fused objects in world and environment coordinates |

An object in `/latest`:

```json
{"id": 12, "class": "person", "subtype": "child", "label": "Child", "score": 0.83,
 "box": [588, 297, 601, 337], "polygon": [[590, 297], ...],
 "direction": "crossing_left", "distance_m": 27.4,
 "dimensions": {"length": 0.4, "width": 0.4, "height": 1.21}, "size_source": "measured",
 "world": [3.2, 24.1, 0.0], "position": {"x": 3.2, "y": 24.1, "z": 0.0},
 "speed_mps": 1.31, "heading": 266.0, "age_s": 6.4}
```

## Extending

**More object types.** Add an entry to `data/object_classes.json`:

```json
{"key": "bicycle", "label": "Bicycle", "detector_labels": ["bicycle"], "group": "two_wheeler",
 "dimensions": {"length": 1.8, "width": 0.6, "height": 1.1}, "subtypes": []}
```

Any label the detector already knows is picked up when the server
restarts. The COCO model also knows motorcycle, bicycle, train, dog and
others. Give it a colour by adding `--bicycle: #hex;` to
`src/static/ovm/ovm.css`, otherwise it is grey. Subtypes split a class by
measured height (`min_height`, `max_height`, optional `dimensions`).

**Another model.** Implement `Detector` in
`src/models/<type>/<name>/`, register it with
`@register("<name>")`, keep its weights in `data/models/<name>/`, and set
`OVM_DETECTOR=<name>`. Models trained for new objects only need their labels
listed in the registry.

## Accuracy

`python manage.py eval_demo` on the demo plaza (both 20 s clips, 15 fps,
1280×720, yolo11n-seg at 960 px, CPU). Ground truth is known exactly. An
object counts as found when it's placed within 3 m of its true position.

| | North camera | East camera | Fused |
|---|---|---|---|
| Objects found | 84.9 % | 99.6 % | 86.2 % |
| Class correct | 100 % | 100 % | |
| Child vs adult correct | 99.1 % | 99.0 % | |
| Direction correct | 97.3 % | 84.4 % | |
| Position error, median / 90th percentile | 0.29 / 1.25 m | 0.27 / 1.33 m | |
| ID switches | 2 | 2 | |
| Duplicates per frame | | | 0.007 |

Calibration from the eight markers recovers both camera poses to within
0.05° and 1 cm.

**What the misses are:** nearly all of the north camera's misses are the
bus while it's 45–87 m away. There it is under about 60 px tall, and the
nano model doesn't detect it. Once detected, it's placed within 1 m. The
east camera's direction errors are mostly diagonal walking, where "crossing"
and "approaching" are both partly true.

**Limits to know about:**

- **Ground:** the ground is a flat plane. Slopes or steps between the
  camera and the object shift positions.
- **Lenses:** lens distortion is not modelled.
- **Object positions:**
  - A vehicle's position is the centre of its footprint seen from the
    camera side, accurate to about half its width.
  - The demo bus is a cutout of a bus seen from the front. Real side views
    will place better.
- **Sizes:**
  - Measured heights need the whole object in view.
  - Uncalibrated distances are only as good as "average height" is for
    that object.
- **Speed:** on CPU the pipeline handles about 6 frames per second per
  feed. For more cameras or more frames per second, use an NVIDIA GPU
  (install the CUDA build of torch).

## Licences

- Three.js: MIT.
- Ultralytics YOLO (the default detector and its weights): **AGPL-3.0**.
  For closed-source or commercial use, either get an Ultralytics enterprise
  licence or plug in a differently licensed model through the `Detector`
  interface.
