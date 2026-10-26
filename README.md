# Object Vision Mapping (OVM)

OVM turns ordinary fixed cameras into a live map of the people and vehicles in
a place. It shows where each one is (latitude/longitude outdoors, floor-plan
coordinates indoors), what it is, how big it is and which way it's going. It
serves that to your own apps over an API.

- **Objects:** people (adult / child), cars, buses, trucks and more (the COCO
  classes).
- **Cameras:** network RTSP cameras (e.g. a Tapo C100) and USB, built-in or
  virtual (OBS) cameras. One environment can use several cameras.
- **Runs on:** a Windows PC. An Intel or NVIDIA GPU is used automatically when
  there is one.

Docs: [Remote API](docs/REMOTE_API.md) · [Technical details](docs/TECHNICAL.md)

---

## What it can do

- **Watch cameras live:** any configured or plugged-in camera, at its own
  frame rate.
- **Detect and outline objects:** each object gets an outline (segmentation
  mask), class, confidence, estimated size (L × W × H), distance from the
  camera and an adult/child split for people. Classes can be toggled on and
  off.
- **Put every object on a map:**
  - **Outdoors:** on satellite or street maps.
  - **Indoors:** on floor plans you draw to scale (m, cm, mm, ft, in, yd),
    or trace over an image.
  - **Calibration:** place a camera by clicking 4 or more spots in its video
    and the same spots on the map. OVM then estimates the camera's position,
    height, heading and tilt, and shows everything in 2D or 3D.
- **Combine cameras:** an object seen by two cameras of one environment is
  reported once.
- **Tracking (optional, per environment):**
  - **On:** stable ids, speed, heading from movement, and positions predicted
    ahead between results.
  - **Off (default):** every result reports exactly what is in the latest
    frame, so fast or faint objects (especially people) are never held back.
- **Serve apps over an API:**
  - **Access keys:** one per app, optionally limited to some classes (for
    example people only).
  - **Getting results:** poll, long-poll or stream (Server-Sent Events). Each
    result is pushed the moment it exists, about 0.1–0.2 s after the camera
    took the frame.
- **Record activity:** clips are saved automatically when something of the
  chosen classes really moves: it is seen steadily and travels at least 2 m.
  The clip runs from 15 s before to 15 s after. A false detection stuck on one
  spot or a parked car never starts one. This works with tracking on or off.
  You can play clips back with detections drawn on top, download them or
  delete them.
- **Stay fast and light:**
  - **Live video** never waits for detection.
  - **Detection runs on the GPU** when there is one.
  - **Nothing runs when nobody is watching:** detection stops 30 s after
    nobody is watching or connected.

---

## Setup

### 1. Get the code and install it

You need **Python 3.12** (Windows: `py -3.12`). From the folder that contains
`ObjectVisionMapping`:

```powershell
cd ObjectVisionMapping
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
```

On macOS / Linux use `python3.12 -m venv .venv` and `.venv/bin/python` instead
of `.venv\Scripts\python`.

The folders:

| Folder | What's in it |
|---|---|
| `src/` | The code (a Django site) |
| `data/` | Everything the site saves: environments, floor plans, models, recordings, access keys |
| `config/` | Secrets: camera passwords, the site's secret key (never commit `secrets.json`) |
| `docs/` | Documentation |

### 2. Add your cameras and secrets

Copy `config/secrets.example.json` to `config/secrets.json` and fill it in:

```json
{
  "django_secret_key": "a long random string",
  "allowed_hosts": ["localhost", "127.0.0.1", "192.168.1.50"],
  "cameras": {
    "tapo_c100": {
      "name": "Tapo C100", "host": "192.168.1.60", "port": 554,
      "username": "camera-account-user", "password": "camera-account-password",
      "stream": "stream2", "v_flipped": false, "h_flipped": false
    }
  }
}
```

- **Network cameras (RTSP):** go under `"cameras"`.
  - **`stream`:** `stream1` is full resolution; `stream2` is 720p, lighter and
    usually plenty.
  - **Tapo cameras:** use the *camera account* set up in the Tapo app
    (Advanced settings → Camera account), not your TP-Link login.
- **Local cameras** (USB, built-in, OBS virtual camera) are found
  automatically and need no entry.
- **`allowed_hosts`:** lists the names or IP addresses other devices use to
  open the site. Add this PC's LAN address if you open OVM from a phone or
  another computer.
- **Keep secrets in this file:** camera passwords live only here, never in
  code or `data/`.

### 3. Create the models

OVM uses Ultralytics YOLO segmentation models:

| Model | Use |
|---|---|
| `yolo26s-seg` | Default with a GPU: more accurate, still fast |
| `yolo11n-seg` | Default on CPU only: smallest and fastest |
| `yolo11s-seg`, `yolo11m-seg`, `yolo26n-seg`, `yolo26m-seg` | Other sizes: `n` < `s` < `m` in accuracy and cost |

Prepare them ahead of time (recommended):

```powershell
cd src
..\.venv\Scripts\python manage.py prepare_models
```

This:
- **Shows the GPUs found:** prints the GPUs on this machine and picks the model
  OVM will use.
- **Downloads the weights:** saved to `data/models/<model>/<model>.pt`.
- **Converts the model for an Intel GPU:** builds an OpenVINO version for your
  camera's frame size (about 30 s, once). It goes to
  `data/models/<model>/<model>_<h>x<w>_openvino_model`.
- **Measures the speed:** times a few frames so you can see how fast detection
  will run.

More options:

```powershell
# several models, for 1080p and 720p cameras
..\.venv\Scripts\python manage.py prepare_models yolo26s-seg yolo11n-seg --size 1920x1080 --size 1280x720
# a specific GPU
..\.venv\Scripts\python manage.py prepare_models --device intel:gpu.1
```

If you skip this step, the same thing happens automatically the first time a
camera is opened on the Detect page. The first detection then just takes
longer.

### 4. Use a GPU (optional, recommended)

OVM picks the hardware itself (`OVM_DEVICE=auto`). It tries the first NVIDIA
GPU, then the first Intel GPU, then falls back to the CPU. Anything that fails
on a GPU falls back to the CPU instead of stopping.

| Hardware | What to do | Speed (720p frame) |
|---|---|---|
| **Intel GPU** (Iris Xe, Arc) | Nothing: `openvino` is already in `requirements.txt`. Keep the Intel graphics driver up to date | `yolo26s-seg` ≈ 95 ms on Iris Xe, 8–9 results/s |
| **NVIDIA GPU** | Install the CUDA build of PyTorch (below) | Faster still |
| **No GPU** | Nothing | `yolo11n-seg` ≈ 200 ms on the CPU |

**NVIDIA:** `requirements.txt` installs the smaller CPU build of PyTorch.
Replace it with a CUDA build that suits your driver, for example:

```powershell
.venv\Scripts\python -m pip install --force-reinstall torch torchvision --index-url https://download.pytorch.org/whl/cu124
.venv\Scripts\python -c "import torch; print(torch.cuda.is_available(), torch.cuda.device_count())"
```

**Several GPUs:** OVM runs one detector, on one GPU, shared by all cameras.
Choose which GPU with `OVM_DEVICE`:

| `OVM_DEVICE` | Runs on |
|---|---|
| `auto` | First NVIDIA GPU, else first Intel GPU, else CPU (default) |
| `cuda:0`, `cuda:1`, … | That NVIDIA GPU (PyTorch) |
| `intel:gpu`, `intel:gpu.0`, `intel:gpu.1`, … | That Intel GPU (OpenVINO) |
| `intel:cpu` | The CPU through OpenVINO |
| `cpu` | The CPU through PyTorch |

`prepare_models` lists the device names it finds. The Detect page shows what
detection is running on, under the video, e.g. "yolo26s-seg on Intel GPU
(OpenVINO) · 95 ms to detect".

### 5. Launch the website

```powershell
cd src
..\.venv\Scripts\python manage.py runserver 0.0.0.0:7080
```

- **On this computer:** open http://localhost:7080.
- **From another device:** open `http://<this PC's IP>:7080`. Add that IP to
  `allowed_hosts` and allow Python through the Windows firewall.
- **Settings:** set environment variables before starting, e.g. in PowerShell
  `$env:OVM_DEVICE = "cuda:1"`.

The **Home** page lists the getting-started steps, marked done as you finish
them:

1. **Cameras:** check each camera's live view.
2. **Floor plans** (indoors only): draw the space.
3. **Environments:** create one, add its cameras, and give each camera 4 or
   more reference points (click the ground in the video, then the same spot on
   the map). Turn **Track objects** on here if you need ids, speed and
   prediction.
4. **Detect:** watch detections live on the video and on the map or 3D view.
5. **Remote access:** create an access key per app and copy the example code.
   Switch recordings on here too.

### Settings

Set these as environment variables before starting the server:

| Variable | Default | What it does |
|---|---|---|
| `OVM_DEVICE` | `auto` | Where detection runs (see above) |
| `OVM_DETECTOR` | best for the device | Model: `yolo26s-seg`, `yolo11n-seg`, … |
| `OVM_IMGSZ` | `960` | Detection size in pixels; larger finds smaller, farther objects but is slower |
| `OVM_MAX_FPS` | `15` | Most detections per second per camera |
| `OVM_FEED_IDLE_SECONDS` | `30` | Detection stops this long after nobody is watching or connected |
| `OVM_DATA_DIR` / `OVM_CONFIG_DIR` | `data/`, `config/` | Other places for data and secrets |
| `OVM_DEBUG` | `1` | `0` for production-style error pages |

---

## Pages

| Page | What it does |
|---|---|
| `/` Home | What OVM does, your setup progress, environments and settings |
| `/cameras/` Cameras | Watch any camera live, with its details |
| `/detect/` Detect | Pick an environment and camera. Shows objects with outlines and class toggles; each object's size, distance, direction and location; and a map / 3D view with the objects placed live |
| `/environments/` Environments | Put a camera's video next to an outdoor map (satellite / street) or a floor plan. Click a spot in the video, then the same spot on the map, to make a reference point. Each camera's position, height and angles are estimated from 4+ points. "Track objects" turns tracking on for the environment |
| `/plans/` Floor plans | Draw indoor maps in m, cm, mm, ft, in or yd (walls, rooms, doors, windows, boxes, labels), or trace over an uploaded image after setting its scale |
| `/remote/` Remote access | Access keys per environment (optionally limited to some object classes), live API, how to connect, recording settings |
| `/recordings/` Saved recordings | Clips saved around movement; play them (with detections drawn on top), download or delete |

The public API is described in [docs/REMOTE_API.md](docs/REMOTE_API.md).

## Code

```
src/
  simulation/          cameras, environments, floor plans, Home page
    camera.py          RTSP + local cameras; one shared reader per camera (newest frame only)
    environment.py     Camera / ReferencePoint / Environment; pixel -> map (homography); pose estimate
    plans.py           floor plan storage and validation
  vision/              detection workers (detect -> optional tracking -> estimate), Detect page,
                       prepare_models command
  remote/              access keys, live API, recorder, recordings
  models/              detector (YOLO segmentation), tracker, object classes, orientation
data/
  environments/<env>.json      cameras, reference points, tracking on/off (+ the floor plan an indoor environment uses)
  plans/<plan>.json            floor plans (+ uploaded images)
  recordings/<env>/            clips: .webm video, .jpg thumbnail, .json timeline of detections
  remote.json                  access keys (hashed) and recording settings
  models/<model>/              detector weights and GPU (OpenVINO) versions
config/secrets.json            camera credentials and site secret (not committed)
```

## How it works, briefly

- **Pixels to places:**
  - **The mapping:** 4 or more reference points define a homography from
    the image to the ground. It's computed in metres (lat/lon, or plan units,
    converted to metres), so it's accurate to millimetres for good points.
  - **Bottom-centre:** an object's location is the bottom-centre of its box,
    where it touches the ground.
  - **Different resolution:** streams of another resolution are rescaled
    automatically.
- **Camera pose:** the camera's focal length, then its position and angles,
  are estimated from the same points. That pose gives distances, real
  heights (adult vs child) and the 3D view.
- **Confidence:** detections below 0.3 are dropped, or below 0.25 for people,
  who are the priority and often score low when far away or partly hidden.
- **Headings:** headings come from real motion (with tracking on), or for
  vehicles from the shape of their outline on the ground, snapped to the
  street grid the system learns.
- **Speed:**
  - **Video:** the camera's reader thread runs at the camera's frame rate.
    Detection always takes the newest frame, and the Detect page draws the
    latest detections over the live video, so the video never waits.
  - **API clients:** each result is built once and pushed to every client the
    moment it exists. `?hz=20` adds predicted positions between results
    (environments with tracking on).
- **Compute only when needed:**
  - **Detection:** runs only while someone is watching (Detect page) or
    connected (API), and stops 30 s after the last request.
  - **Recordings:** made while detection runs (an app is connected or the
    Detect page is open), from 15 s before movement to 15 s after it.
    Movement means seen steadily and travelling 2 m on the ground. A stop
    shorter than 15 s doesn't end the clip. Video is encoded in the
    background, so it never holds up detection. Long activity is saved in
    1-minute clips.

## Tests

`..\.venv\Scripts\python manage.py test tests` (from `src/`) runs 77 tests.
Nothing in them touches real cameras or the real `data/` folder. They cover:
- geometry and the pixel-to-map mapping against simulated cameras (outdoors
  and on floor plans), and the pose estimate
- vehicle orientation
- floor plans
- access keys and class limits, merging objects seen by two cameras, and the
  live stream
- live results reaching clients at once (long polling), prediction ahead, the
  detector's input shape, and GPU selection
- tracking on/off per environment, and faint or fast objects shown at once
- what starts a recording (steady, 2 m of movement; not noise on one spot,
  not parked cars), with or without tracking
- recording clip rules (before, after, 1-minute pieces) with real video
  encoding, and playback with seeking
- local cameras

---

## Original requirements

The brief OVM was built from.

### Phase 1
- Create a Django application inside ObjectVisionMapping/src.
- We should be able to have a 3D space where we can set reference points and fixed camera points.
- This "Environment" should support multiple units like using geo-spatial points with height in meters or predefining the space in custom units.
- Cameras should have height, location on this map, the (360) direction, field of view, and tilt fields associated with it.
- The website should be able to create "Environments" and store them under ObjectVisionMapping/data/environments/env_name (as JSON).
- The website should be able to view and explore the selected environment.
- All this under the domain.com/environments path.
- All things related to the Environment base should be in a class called "Environment". It can have subclasses to handle reference points, camera locations and more.
- We will later pair this with actual specific cameras.

### Phase 2
- A place where we can select the camera, view the feed, and have a model use segmentation to detect various objects and classify them, with toggles for the different objects and live metadata.
- Estimate the size of these objects using averages (a car's average size compared to a truck, a person or a kid).
- Object direction: whether a vehicle is coming towards us or moving across, and the correct orientation in the environment simulation.
- Models stored under /data/models/xyzmodel, code under /src/models/model_type/xyz.
- Tracking objects while they are in frame, each with its own ID kept across neighbouring frames (now optional per environment).

### Phase 3
- Testing an environment: select an environment and its camera(s), then select the pixel coordinates in the feed that the reference points map to, taking the camera's resolution into account.
- See the environment, the camera feeds, and the objects in the environment where they should be.
