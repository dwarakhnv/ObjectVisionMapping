# Using Object Vision Mapping

This guide walks through the three parts of the site: **Environments** (the
3D space), **Live** (detection on a camera feed) and **Mapping** (calibrating
cameras and seeing objects placed in the environment).

Start the site from `ObjectVisionMapping/src`:

```
..\.venv\Scripts\python manage.py runserver 0.0.0.0:7080
```

then open http://localhost:7080. To get a ready-made example, run
`..\.venv\Scripts\python manage.py make_demo` once first (see
[The demo plaza](#the-demo-plaza)).

---

## 1. Create an environment (`/environments`)

An environment is a space you want to map objects into: a junction, a car
park, a warehouse floor, a lab. Choose **New environment** and pick how
positions are written:

| Coordinates | Positions | Good for |
|---|---|---|
| **Custom units** | x (east / right on the plan), y (north / up on the plan), z (up), in a unit you name. Set *metres per unit* (feet = 0.3048). | Floor plans, sites measured with a tape or laser, anything without GPS |
| **Geo** | Latitude, longitude (WGS84) and altitude in metres. An origin anchors a local east/north/up frame. | Outdoor sites surveyed with GPS or read off a map |

Everything is stored in `data/environments/<name>/environment.json`. The
file is readable JSON, so it's easy to check or edit by hand.

### Explore and edit

The page shows the environment in 3D. Drag to orbit, right-drag to pan,
scroll to zoom, and use **Top** for a plan view. The panel has three tabs:

- **Reference points**: known spots the cameras can see, such as painted
  marks, pole bases, kerb corners or lane-line ends. Choose **Add point**,
  then click the ground to place it, or type exact coordinates. Points can
  also have a height (z or altitude).
- **Cameras**: where each fixed camera stands, its mount height, its
  **heading** (compass direction it points, 0° = north, clockwise), how far
  it **tilts** down, **roll**, horizontal **field of view**, and its native
  **resolution**.
  - **Move on map** places the camera mast.
  - **Aim at map point** points the camera at a spot.
  - **Look through** shows the 3D view from the camera.
  - The blue area on the ground is what the camera sees.
- **Environment**: name, units, origin, and the size of the area to show.

Changes are previewed straight away. **Save** writes the file.

### Pair a camera with a real source

Under a camera's **Source**, choose one of:

| Source | Location |
|---|---|
| Video file | A file name in `data/sources/` (upload from the Live page) or an absolute path |
| Stream URL | `rtsp://` or `http(s)://`, e.g. `rtsp://{username}:{password}@10.0.0.5/stream1` |
| Local device | A webcam index (`0` is the first) |
| Still image | An image in `data/sources/` |

For streams, put the credentials in `config/secrets.json` (copy
`config/secrets.example.json`) under `"cameras"`, and enter that entry's
name as the camera's **Secret name**. `{username}` and `{password}` in the
URL are filled in when the stream opens, so passwords never go into the
environment file.

---

## 2. Watch a feed (`/`)

Pick a camera (or an uploaded file) and the server starts reading it, finds
objects with an instance-segmentation model, and tracks them.

- **Class toggles** (Car, Bus, Truck, Person) show or hide each type and
  count what's in view.
- **Masks / Boxes / Labels / Trails** switch the overlay layers.
- Every object keeps its **ID** while it stays in view. Brief misses (up
  to 15 processed frames, about 2 seconds) don't change the ID.
- Click an object (in the image or the list) for its details:
  - class and subtype
  - confidence, and how long it has been tracked
  - direction
  - distance and size, and whether the size was measured or is the class average
  - for calibrated cameras: position, speed and compass heading

**Direction** is one of: approaching, moving away, crossing left, crossing
right or stationary.

**Size and distance:**

- **Uncalibrated camera:** distance comes from the class's average height
  (an object of known height that appears *h* pixels tall is at
  focal length × height ÷ *h*). The size shown is the class average.
- **Calibrated camera:** the object's foot point is located on the ground,
  which gives position and distance, and the top of the box gives its real
  height. The height separates **children from adults** (under 1.4 m) and
  **pickups/vans from trucks** (under 2.4 m).

Feeds stop on their own 30 seconds after nobody is watching.

---

## 3. Calibrate cameras (`/mapping/<environment>/`)

Calibration finds exactly where a camera is and where it points, by matching
reference points to the pixels where they appear.

1. Pick the camera. The image is a **live frame** from its source, or an
   image you **upload** (a still from the camera works when it isn't
   connected yet).
2. Select a reference point in the list and click where it appears. Use the
   magnifier for precision, and drag markers to adjust them. After each
   click the next unplaced point is selected.
3. Place **at least 4** points, spread across the ground area you care
   about. Points far apart and at different distances give the best result.
   With **6 or more**, tick *Also estimate the field of view* if you don't
   know the lens's exact field of view.
4. Choose **Solve camera pose**. The camera's position, height, heading,
   tilt and roll are updated and saved, and the image shows:
   - a **ground grid** as the solved camera sees it. It should sit flat on
     the real ground.
   - **circles** where each reference point should appear, joined to your
     click. A long line means a misplaced click or a mis-measured point.
   - the error per point and on average. Under 3 px per 1000 px of image
     is a good fit.

**Resolution:** clicks are stored as fractions of the image width and
height, together with the resolution of the image used. A camera that also
streams at a lower resolution of the same shape (1920×1080 and 1280×720)
needs no recalibration. If the image has a different shape from the
camera's configured resolution, solving adopts the image's resolution.

---

## 4. The live map (`/mapping/<environment>/live/`)

The environment in 3D, with:

- a live tile for every camera that has a source
- every object from the **calibrated** cameras, drawn at its position as a
  box of its estimated size, turned to its direction of travel, with a
  short trail
- **multi-camera merging**: one object seen by several cameras appears once,
  marked "2 cams" and so on, with one ID that stays while any camera keeps
  seeing it

The same data is available as JSON at `/api/mapping/<environment>/objects`.

---

## The demo plaza

`python manage.py make_demo` builds a test environment with known answers:

- `data/environments/demo_plaza/`: a flat plaza with eight painted markers
  (P1–P8) and two cameras, already calibrated from the markers
- `data/sources/demo_plaza_north.mp4` and `demo_plaza_east.mp4`: the plaza
  rendered from each camera. Two adults, a child and a bus move along known
  paths. They are real photographic cutouts, so the detector sees genuine
  people and a genuine bus.
- `data/sources/demo_plaza_truth.json`: where everything really was, frame
  by frame

`python manage.py eval_demo` runs the whole pipeline on both videos and
scores it against the truth. See [TECHNICAL.md](TECHNICAL.md#accuracy) for
the latest numbers.
