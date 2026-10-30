"""Remote access (keys, public API, merging cameras), recordings, floor plans and local cameras.

Run from src/:  ..\\.venv\\Scripts\\python manage.py test tests
Nothing here touches real cameras or the real data/ folder.
"""
import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings
from shapely.geometry import Point

from simulation.environment import Camera, Environment, PlanSpace, ReferencePoint


class TempData(SimpleTestCase):
    """Each test gets its own empty data folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "environments").mkdir()
        self.override = override_settings(DATA_DIR=self.tmp)
        self.override.enable()
        self.patches = [mock.patch("remote.views.ENV_DIR", self.tmp / "environments"),
                        mock.patch("remote.live.ENV_DIR", self.tmp / "environments"),
                        mock.patch("simulation.views.ENV_DIR", self.tmp / "environments"),
                        mock.patch.dict("remote.live._states", clear=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, name="Lot", cameras=("cam a", "cam b"), tracking=False):
        cams = []
        for c in cameras:
            pts = [ReferencePoint((x, y), Point(-78.878 + x * 1e-6, 35.685 + y * 1e-6)) for x, y in
                   [(100, 600), (1100, 620), (900, 200), (300, 220), (640, 400)]]
            cams.append(Camera(c, (1280, 720), pts, source="src_" + c.replace(" ", "_")))
        e = Environment(name, cams, tracking=tracking)
        e.save(self.tmp / "environments")
        return e


class FakeWorker:
    def __init__(self, objects, status="running"):
        self.latest = {"status": status, "objects": objects, "frame_id": 1}


def obj(i, cls, lat, lon, score=0.9, moving=False):
    return {"id": i, "class": cls, "label": cls.title(), "score": score, "geo": {"lat": lat, "lon": lon},
            "dimensions": {"length": 4.5, "width": 1.8, "height": 1.5}, "direction": "approaching" if moving else "stationary",
            "heading": 90.0, "speed_mps": 3.0 if moving else 0.0, "age_s": 2.0, "box": [0, 0, 10, 10]}


class RemoteApiTests(TempData):
    def create_key(self, env, classes=()):
        r = self.client.post("/api/remote/%s/subscriptions" % env, json.dumps({"name": "App", "classes": list(classes)}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def test_keys_are_shown_once_and_stored_hashed(self):
        self.env()
        r = self.create_key("lot", ["person"])
        token = r["token"]
        stored = (self.tmp / "remote.json").read_text()
        self.assertNotIn(token, stored)
        self.assertNotIn("token_hash", json.dumps(r["subscription"]))
        listing = self.client.get("/api/remote/lot").json()
        self.assertEqual(listing["subscriptions"][0]["hint"], token[-4:])
        self.assertNotIn("token_hash", json.dumps(listing))

    def test_public_api_auth_and_class_limits(self):
        self.env()
        token = self.create_key("lot", ["car"])["token"]
        workers = {"@lot.cam_a": FakeWorker([obj(1, "car", 35.685, -78.878), obj(2, "person", 35.6851, -78.878)])}
        with mock.patch("remote.live.feeds.get_worker", side_effect=lambda fid: workers.get(fid, FakeWorker([], "starting"))):
            self.assertEqual(self.client.get("/api/v1/environments/lot/objects").status_code, 401)
            self.assertEqual(self.client.get("/api/v1/environments/lot/objects", HTTP_AUTHORIZATION="Bearer nope").status_code, 403)
            self.assertEqual(self.client.get("/api/v1/environments/nowhere/objects", HTTP_AUTHORIZATION="Bearer " + token).status_code, 404)
            r = self.client.get("/api/v1/environments/lot/objects", HTTP_AUTHORIZATION="Bearer " + token)
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertEqual([o["class"] for o in data["objects"]], ["car"])  # the person is filtered out
            self.assertEqual(set(data["objects"][0]["position"]), {"lat", "lon"})
            self.assertEqual(data["cameras"]["cam_b"]["status"], "starting")
            self.assertEqual(self.client.get("/api/v1/environments/lot/objects?classes=person&token=" + token).status_code, 403)
            r = self.client.get("/api/v1/environments/lot/objects", HTTP_X_API_KEY=token)
            self.assertEqual(r.status_code, 200)

    def test_revoked_and_disabled_keys_stop_working(self):
        self.env()
        made = self.create_key("lot")
        sub_id, token = made["subscription"]["id"], made["token"]
        with mock.patch("remote.live.feeds.get_worker", return_value=FakeWorker([])):
            ok = lambda: self.client.get("/api/v1/environments/lot/objects", HTTP_AUTHORIZATION="Bearer " + token).status_code  # noqa: E731
            self.assertEqual(ok(), 200)
            self.client.patch("/api/remote/subscriptions/" + sub_id, json.dumps({"enabled": False}), content_type="application/json")
            self.assertEqual(ok(), 403)
            self.client.patch("/api/remote/subscriptions/" + sub_id, json.dumps({"enabled": True}), content_type="application/json")
            self.assertEqual(ok(), 200)
            self.client.delete("/api/remote/subscriptions/" + sub_id)
            self.assertEqual(ok(), 403)

    def test_key_for_one_environment_does_not_open_another(self):
        self.env("Lot")
        self.env("Yard")
        token = self.create_key("lot")["token"]
        r = self.client.get("/api/v1/environments/yard/objects", HTTP_AUTHORIZATION="Bearer " + token)
        self.assertEqual(r.status_code, 403)

    def test_cameras_seeing_the_same_object_are_merged_with_stable_ids(self):
        self.env(tracking=True)
        from remote import live
        a = FakeWorker([obj(5, "car", 35.68500, -78.87800), obj(6, "car", 35.68530, -78.87800)])
        b = FakeWorker([obj(9, "car", 35.685005, -78.878002)])  # ~0.6 m from cam a's car 5
        with mock.patch("remote.live.feeds.get_worker", side_effect=lambda fid: a if fid.endswith("cam_a") else b):
            first = live.snapshot("lot")
            self.assertEqual(len(first["objects"]), 2)
            merged = [o for o in first["objects"] if len(o["cameras"]) == 2]
            self.assertEqual(len(merged), 1)
            second = live.snapshot("lot")
        self.assertEqual([o["id"] for o in first["objects"]], [o["id"] for o in second["objects"]])

    def test_stream_sends_events(self):
        self.env()
        token = self.create_key("lot")["token"]
        with mock.patch("remote.live.feeds.get_worker", return_value=FakeWorker([obj(1, "car", 35.685, -78.878)])):
            r = self.client.get("/api/v1/environments/lot/stream?token=" + token)
            self.assertEqual(r["Content-Type"], "text/event-stream")
            chunks = iter(r.streaming_content)
            self.assertIn(b"retry", next(chunks))
            event = next(chunks).decode()
            r.close()
        self.assertTrue(event.startswith("event: objects\ndata: "))
        self.assertEqual(json.loads(event.split("data: ", 1)[1])["objects"][0]["class"], "car")

    def test_recording_settings(self):
        self.env()
        r = self.client.put("/api/remote/lot/recording", json.dumps({"enabled": False, "classes": ["person"]}),
                            content_type="application/json")
        self.assertEqual(r.json()["recording"], {"enabled": False, "classes": ["person"], "region": []})
        r = self.client.put("/api/remote/lot/recording", json.dumps({"enabled": True, "classes": ["dragon"]}),
                            content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/remote/").status_code, 200)
        self.assertEqual(self.client.get("/recordings/").status_code, 200)

    def test_recording_region(self):
        self.env()
        put = lambda body: self.client.put("/api/remote/lot/recording", json.dumps(body), content_type="application/json")  # noqa: E731
        put({"classes": ["person"]})
        square = [{"lat": 35.685, "lon": -78.878}, {"lat": 35.685, "lon": -78.8779},
                  {"lat": 35.6851, "lon": -78.8779}, {"lat": 35.6851, "lon": -78.878}]
        r = put({"region": square}).json()["recording"]
        self.assertEqual((r["classes"], len(r["region"])), (["person"], 4))  # only the region changed
        self.assertEqual(put({"enabled": False}).json()["recording"]["region"], r["region"])
        self.assertEqual(put({"region": square[:2]}).status_code, 400)                       # 2 corners
        self.assertEqual(put({"region": [square[0]] * 3}).status_code, 400)                  # no area
        self.assertEqual(put({"region": [{"lat": 95, "lon": 0}] + square[1:]}).status_code, 400)
        self.assertEqual(put({"region": []}).json()["recording"]["region"], [])
        self.assertEqual(len(self.client.get("/api/remote/lot").json()["cameras"][0]["points"]), 5)


class FakeCam:
    """Stands in for a shared camera: a new numbered frame every 50 ms."""

    def __init__(self):
        self.n = 0
        self.key = "fake"
        self.paused_until = 0.0

    def latest(self, after=0, timeout=1.0):
        time.sleep(0.05)
        if time.time() < self.paused_until:  # a camera hiccup: the same frame again
            return self.n, np.full((72, 128, 3), self.n % 255, np.uint8)
        self.n += 1
        f = np.full((72, 128, 3), self.n % 255, np.uint8)
        return self.n, f


class RecorderTests(TempData):
    def setUp(self):
        super().setUp()
        from remote import recorder
        self.rec_mod = recorder
        self.consts = mock.patch.multiple(recorder, PRE_SECONDS=1.0, POST_SECONDS=1.0, SEGMENT_SECONDS=3.0, FPS=10,
                                          MIN_MOVE_M=1.0)
        self.consts.start()
        self.cam = FakeCam()
        self.cams = [mock.patch("simulation.camera.acquire", return_value=self.cam),
                     mock.patch("simulation.camera.release")]
        for p in self.cams:
            p.start()

    def tearDown(self):
        for p in self.cams:
            p.stop()
        self.consts.stop()
        super().tearDown()

    def feed(self, rec, seconds, moving):
        """A person standing still, or walking at 3 m/s; ids change every result, as without tracking."""
        end = time.time() + seconds
        while time.time() < end:
            if moving:
                self.x = getattr(self, "x", 0.0) + 0.3
            o = dict(obj(int(time.time() * 1000) % 997, "person", 35.685, -78.878), world=[getattr(self, "x", 0.0), 10.0, 0.0])
            rec.observe([o], [], "geo")
            time.sleep(0.1)

    def test_clip_has_pre_roll_and_post_roll(self):
        rec = self.rec_mod.Recorder("lot", "cam_a", "fake")
        longer = mock.patch.object(self.rec_mod, "SEGMENT_SECONDS", 10.0)
        longer.start()
        self.addCleanup(longer.stop)
        self.feed(rec, 1.5, moving=False)   # quiet: only buffered
        self.assertEqual(self.rec_mod.list_recordings("lot"), [])
        self.feed(rec, 1.0, moving=True)    # activity
        self.feed(rec, 1.8, moving=False)   # quiet again: the clip ends ~1 s later
        rec.close()
        recs = self.rec_mod.list_recordings("lot")
        self.assertEqual(len(recs), 1, recs)
        r = recs[0]
        # ~1 s before + the movement (from once it has gone 1 m, until ~2 s after it stops) + ~1 s after
        self.assertGreater(r["duration_s"], 2.2)
        self.assertLess(r["duration_s"], 5.0)
        self.assertEqual(r["seen"], {"person": 1})
        self.assertTrue(r["thumbnail"])
        video = self.rec_mod.recording_path("lot", r["id"], "video")
        cap = cv2.VideoCapture(str(video))
        n = 0
        while cap.read()[0]:
            n += 1
        cap.release()
        self.assertAlmostEqual(n / 10.0, r["duration_s"], delta=0.3)

    def test_camera_pause_is_noted_in_the_clip(self):
        rec = self.rec_mod.Recorder("lot", "cam_a", "fake")
        self.feed(rec, 1.2, moving=True)
        self.cam.paused_until = time.time() + 1.5
        self.feed(rec, 2.0, moving=True)
        rec.close()
        stalls = [s for r in self.rec_mod.list_recordings("lot") for s in r["stalls"]]
        self.assertEqual(len(stalls), 1, stalls)
        self.assertAlmostEqual(stalls[0][1], 1.5, delta=0.3)

    def test_first_working_encoder_is_used(self):
        good = ("good", ".webm", lambda p, s: self.rec_mod._CvWriter(p, s, cv2.CAP_ANY, "VP80"))

        def broken(p, s):
            raise RuntimeError("no such encoder here")
        with mock.patch.object(self.rec_mod, "_codec", None),                 mock.patch.object(self.rec_mod, "_encoders", return_value=[("broken", ".mp4", broken), good]):
            self.assertEqual(self.rec_mod.codec()[0], "good")

    def test_long_activity_is_split_into_segments(self):
        rec = self.rec_mod.Recorder("lot", "cam_a", "fake")
        self.feed(rec, 7.5, moving=True)
        rec.close()
        recs = sorted(self.rec_mod.list_recordings("lot"), key=lambda r: r["start"])
        self.assertGreaterEqual(len(recs), 2)
        self.assertFalse(recs[0]["continued"])
        self.assertTrue(all(r["continued"] for r in recs[1:]))
        self.assertTrue(all(r["duration_s"] <= 3.3 for r in recs[:-1]))

    def test_movement_outside_the_region_is_not_recorded(self):
        rec = self.rec_mod.Recorder("lot", "cam_a", "fake")
        lat, lon = 35.685, -78.878
        region = [{"lat": lat - 1e-4, "lon": lon - 1e-4}, {"lat": lat - 1e-4, "lon": lon + 1e-4},
                  {"lat": lat + 1e-4, "lon": lon + 1e-4}, {"lat": lat + 1e-4, "lon": lon - 1e-4}]
        end, x = time.time() + 2.0, 0.0
        while time.time() < end:  # a "person" walking 3 m/s, across the street: outside the region
            x += 0.3
            o = dict(obj(1, "person", lat + 5e-4, lon), world=[x, 50.0, 0.0])
            rec.observe([o], [], "geo", region)
            time.sleep(0.1)
        rec.close()
        self.assertEqual(self.rec_mod.list_recordings("lot"), [])
        inside = self.rec_mod.region_test(region, "geo")
        self.assertTrue(inside(obj(1, "person", lat, lon)))
        self.assertFalse(inside(obj(1, "person", lat + 5e-4, lon)))
        self.assertFalse(inside({"class": "person"}))           # no map position: can't be inside
        self.assertTrue(self.rec_mod.region_test([], "geo")({"class": "person"}))

    def test_recordings_api_plays_with_ranges_and_deletes(self):
        rec = self.rec_mod.Recorder("lot", "cam_a", "fake")
        self.feed(rec, 1.2, moving=True)
        rec.close()
        self.env()
        r = self.client.get("/api/recordings/lot").json()["recordings"][0]
        rid, url = r["id"], "/api/recordings/lot/" + r["file"]
        self.assertEqual(r["file"], rid + self.rec_mod.codec()[1])
        full = b"".join(self.client.get(url).streaming_content)
        part = self.client.get(url, HTTP_RANGE="bytes=10-19")
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part["Content-Type"], "video/mp4" if r["file"].endswith(".mp4") else "video/webm")
        self.assertEqual(part.content, full[10:20])
        self.assertEqual(part["Content-Range"], "bytes 10-19/%d" % len(full))
        self.assertIn("timeline", self.client.get("/api/recordings/lot/" + rid).json())
        self.assertEqual(self.client.get("/api/recordings/lot/../../remote.webm").status_code, 404)
        self.assertEqual(self.client.delete("/api/recordings/lot/" + rid).status_code, 200)
        self.assertEqual(self.client.get("/api/recordings/lot").json()["recordings"], [])


class ActivityTests(SimpleTestCase):
    """What starts a recording: steady sightings that travel 2 m, with or without tracking."""

    def run_check(self, positions, classes=(), seconds=20.0, dt=0.12):
        """positions(t) -> [(class, x, y)] for each result; returns the times something counted as moving."""
        from remote.recorder import ActivityCheck
        check, hits = ActivityCheck({"car": "vehicle", "truck": "vehicle"}), []
        for i in range(int(seconds / dt)):
            t = 1000.0 + i * dt
            objs = [{"id": 1 + (i * 7 + k) % 13, "class": c, "world": [x, y, 0.0]} for k, (c, x, y) in enumerate(positions(i * dt, i))]
            if check.update(t, objs, list(classes), "geo"):
                hits.append(i * dt)
        return hits

    def test_noise_on_one_spot_never_counts(self):
        rng = np.random.default_rng(1)
        jitter = lambda: rng.normal(0, 0.3)  # noqa: E731  feet of a far, false "person" wobble
        self.assertEqual(self.run_check(lambda t, i: [("person", 30 + jitter(), 40 + jitter())]), [])
        # flickering: seen in one result of three, sometimes a little off
        self.assertEqual(self.run_check(lambda t, i: [("person", 12 + jitter(), 5)] if i % 3 == 0 else []), [])

    def test_walking_person_counts_and_again_after_a_short_stop(self):
        def walk(t, i):  # 1.4 m/s, stops for 3 s at t = 6..9, walks on
            x = 1.4 * min(t, 6) + 1.4 * max(0, t - 9)
            return [("person", x, 10)] if i % 5 else []  # missed in every 5th result
        hits = self.run_check(walk, seconds=14)
        self.assertTrue(hits and hits[0] < 2.5)              # counted within ~2 m of walking
        self.assertFalse(any(8.0 < h < 9.0 for h in hits))   # standing still: the clip's 15 s after-roll covers it
        self.assertTrue(any(9.0 < h < 10.0 for h in hits))   # walks on: counts again straight away

    def test_parked_cars_do_not_count_but_a_driving_one_does(self):
        parked = [("car", 5.0 * k, 0) for k in range(4)]
        self.assertEqual(self.run_check(lambda t, i: parked, seconds=10), [])
        hits = self.run_check(lambda t, i: parked + [("truck" if i % 4 == 0 else "car", 8 * t, 6)], seconds=4)
        self.assertTrue(hits and hits[0] < 1.0)
        self.assertEqual(self.run_check(lambda t, i: [("car", 8 * t, 6)], classes=["person"], seconds=4), [])


class FloorPlanTests(TempData):
    PLAN = {"name": "Office", "units": "ft", "width": 40, "height": 30,
            "shapes": [{"type": "wall", "points": [[0, 0], [40, 0]], "thickness": 0.5},
                       {"type": "room", "points": [[0, 0], [20, 0], [20, 30]], "name": "Hall"},
                       {"type": "door", "points": [[20, 20], [20, 23]]},
                       {"type": "label", "points": [[30, 15]], "text": "Lobby"}]}

    def test_plan_api_and_validation(self):
        r = self.client.post("/api/plans", json.dumps(self.PLAN), content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["slug"], "office")
        bad = dict(self.PLAN, shapes=[{"type": "door", "points": [[0, 0]]}])
        self.assertEqual(self.client.put("/api/plans/office", json.dumps(bad), content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/plans", json.dumps(dict(self.PLAN, name="Other", units="furlongs")),
                                          content_type="application/json").status_code, 400)
        self.assertEqual(self.client.get("/plans/").status_code, 200)

    def test_indoor_environment_maps_pixels_to_plan_coordinates(self):
        self.client.post("/api/plans", json.dumps(self.PLAN), content_type="application/json")
        from environments import geometry
        # A camera 3 m up in the room; reference points on the floor, in feet.
        sim = geometry.CameraModel([6.0, -9.0, 3.0], 0, 40, 0, 80, 1280, 720)
        ft = 0.3048
        pts = []
        for x, y in [(10, 12), (30, 12), (28, 4), (12, 3), (20, 8)]:
            (u, v), = sim.project(np.array([[x * ft, -y * ft, 0.0]]))[0]
            pts.append({"name": "", "pixel": [u, v], "x": x, "y": y})
        body = {"name": "Office floor", "space": {"kind": "plan", "plan": "office"},
                "cameras": [{"name": "Lobby cam", "source": "x", "resolution": [1280, 720], "reference_points": pts}]}
        r = self.client.post("/api/envs", json.dumps(body), content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        env = r.json()
        self.assertEqual(env["environment"]["space"], {"kind": "plan", "plan": "office", "units": "ft", "meters_per_unit": ft})
        pose = env["analysis"]["cameras"][0]["pose"]
        self.assertAlmostEqual(pose["height_m"], 3.0, places=2)
        self.assertAlmostEqual(pose["x"], 6.0 / ft, places=1)
        self.assertAlmostEqual(pose["y"], 9.0 / ft, places=1)
        cam = Environment.load(self.tmp / "environments", "office_floor").cameras[0]
        self.assertIsInstance(cam.space, PlanSpace)
        (u, v), = sim.project(np.array([[15 * ft, -10 * ft, 0.0]]))[0]
        p = cam.pixel_to_map((u, v))
        self.assertAlmostEqual(p.x, 15, places=2)
        self.assertAlmostEqual(p.y, 10, places=2)
        # A plan in use can't be deleted
        self.assertEqual(self.client.delete("/api/plans/office").status_code, 409)
        # Outdoor points on an indoor map are refused
        body["cameras"][0]["reference_points"][0] = {"pixel": [1, 2], "lat": 35.0, "lon": -78.0}
        self.assertEqual(self.client.put("/api/envs/office_floor", json.dumps(body), content_type="application/json").status_code, 400)

    def test_image_upload_types(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        ok, png = cv2.imencode(".png", np.zeros((10, 10, 3), np.uint8))
        r = self.client.post("/api/plans/images", {"file": SimpleUploadedFile("scan.png", png.tobytes(), "image/png")})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.client.get("/api/plans/images/" + r.json()["file"])["Content-Type"], "image/png")
        r = self.client.post("/api/plans/images", {"file": SimpleUploadedFile("evil.svg", b"<svg onload=alert(1)>", "image/svg+xml")})
        self.assertEqual(r.status_code, 400)


class LocalCameraTests(SimpleTestCase):
    def test_local_cameras_join_the_shared_list(self):
        from simulation import camera
        fake = [{"key": "local_abc", "name": "USB Cam", "index": 0, "backend": 700, "path": "p", "kind": "usb", "vid": "1234", "pid": "5678"}]
        with mock.patch("simulation.camera.load_camera_configs", return_value={}), \
                mock.patch("simulation.camera._enumerate_local", return_value=fake):
            cams = camera.shared_cameras()
            self.assertIn("local_abc", cams)
            self.assertEqual(cams["local_abc"].kind, "usb")
            self.assertEqual(cams["local_abc"].safe_url, "USB camera (1234:5678)")
            r = self.client.get("/api/cameras")
        self.assertEqual([(c["id"], c["kind"]) for c in r.json()["cameras"]], [("local_abc", "usb")])


class SpeedTests(TempData):
    """Live results reach clients the moment they exist, and can be predicted ahead between results."""

    def test_prediction_moves_only_moving_objects(self):
        self.env(cameras=("cam a",), tracking=True)
        from remote import live
        moving = dict(obj(1, "car", 35.685, -78.878, moving=True), heading_source="motion")  # 3 m/s due east
        parked = obj(2, "car", 35.6852, -78.878)
        w = FakeWorker([moving, parked])
        w.latest["captured_at"] = time.time() - 0.5
        with mock.patch("remote.live.feeds.get_worker", return_value=w):
            plain = live.snapshot("lot")
            ahead = live.snapshot("lot", predict=True)
        by_id = lambda snap: {o["id"]: o for o in snap["objects"]}  # noqa: E731
        p0, p1 = by_id(plain)[1]["position"], by_id(ahead)[1]["position"]
        east_m = (p1["lon"] - p0["lon"]) * 111320 * np.cos(np.radians(35.685))
        self.assertAlmostEqual(east_m, 1.5, delta=0.2)          # 3 m/s for ~0.5 s
        self.assertAlmostEqual(p1["lat"], p0["lat"], places=7)
        self.assertTrue(by_id(ahead)[1]["predicted"])
        self.assertFalse(by_id(ahead)[2]["predicted"])
        self.assertEqual(by_id(ahead)[2]["position"], by_id(plain)[2]["position"])
        self.assertNotIn("predicted", by_id(plain)[1])
        self.assertTrue(all(not k.startswith("_") for o in ahead["objects"] for k in o))

    def test_long_poll_returns_as_soon_as_a_new_result_is_published(self):
        self.env(cameras=("cam a",))
        token = RemoteApiTests.create_key(self, "lot")["token"]
        from vision import feeds
        w = feeds.FeedWorker("@lot.cam_a")  # not started: results are published by hand
        w._publish({"frame_id": 1, "status": "running", "objects": [obj(1, "car", 35.685, -78.878)], "captured_at": time.time()})
        url = "/api/v1/environments/lot/objects?token=" + token
        with mock.patch("remote.live.feeds.get_worker", return_value=w):
            seq = self.client.get(url).json()["seq"]
            later = threading.Timer(0.3, lambda: w._publish({"frame_id": 2, "status": "running", "captured_at": time.time(),
                                                             "objects": [obj(1, "car", 35.68501, -78.878)]}))
            later.start()
            t = time.monotonic()
            r = self.client.get(url + "&wait=5&after=%d" % seq).json()
            waited = time.monotonic() - t
            later.join()
        self.assertGreater(r["seq"], seq)
        self.assertLess(waited, 1.5)  # woken by the result, not by the 5 s timeout
        self.assertAlmostEqual(r["objects"][0]["position"]["lat"], 35.68501, places=6)

    def test_live_video_only_for_cameras(self):
        self.env(cameras=("cam a",))
        from vision import feeds
        with mock.patch("vision.feeds.resolve_feed", return_value=({"kind": "shared", "uri": "tapo_c100"}, None, "cam_a")):
            self.assertEqual(feeds.video_camera("@lot.cam_a"), "tapo_c100")
        with mock.patch("vision.feeds.resolve_feed", return_value=({"kind": "file", "uri": "clip.mp4"}, None, "cam_a")):
            self.assertIsNone(feeds.video_camera("@lot.cam_a"))
            self.assertEqual(self.client.get("/api/feeds/@lot.cam_a/video").status_code, 404)

    def test_fast_model_input_keeps_the_frame_shape(self):
        from models.segmentation.yolo.detector import YoloSegDetector
        d = YoloSegDetector.__new__(YoloSegDetector)
        d.imgsz = 960
        self.assertEqual(d.input_shape(720, 1280), (544, 960))   # 16:9, padded to a multiple of 32
        self.assertEqual(d.input_shape(1080, 1920), (544, 960))
        self.assertEqual(d.input_shape(480, 640), (736, 960))
        self.assertEqual(d.input_shape(960, 960), (960, 960))


class TrackingOptionTests(TempData):
    """Tracking is a per-environment choice, off by default; without it nothing waits to be matched."""

    def test_setting_defaults_off_and_is_saved(self):
        e = Environment.from_dict({"name": "X", "cameras": []})
        self.assertFalse(e.tracking)
        self.assertFalse(e.to_dict()["tracking"])
        self.env(tracking=True)
        r = self.client.get("/api/envs/lot").json()
        self.assertTrue(r["environment"]["tracking"])
        r["environment"]["tracking"] = False
        self.client.put("/api/envs/lot", json.dumps(r["environment"]), content_type="application/json")
        self.assertFalse(Environment.load(self.tmp / "environments", "lot").tracking)

    def test_without_tracking_ids_number_the_current_picture(self):
        self.env(cameras=("cam a",))
        from remote import live
        w = FakeWorker([obj(7, "person", 35.685, -78.878, moving=True), obj(8, "car", 35.6852, -78.878)])
        w.latest["captured_at"] = time.time() - 0.5
        with mock.patch("remote.live.feeds.get_worker", return_value=w):
            snap = live.snapshot("lot", predict=True)
        self.assertFalse(snap["tracking"])
        self.assertEqual(sorted(o["id"] for o in snap["objects"]), [1, 2])
        self.assertTrue(all(o["tracked_for_s"] is None and not o["predicted"] for o in snap["objects"]))

    def test_faint_and_fast_objects_are_shown_at_once(self):
        from models.segmentation.base import Detection
        from models.tracking.sort.tracker import Tracker, untracked

        def person(x, score):
            return Detection(label="person", score=score, box=(x, 300, x + 30, 380), class_key="person")
        faint, fast = Tracker(), Tracker()
        self.assertEqual(sum(len(faint.update([person(5 * i, 0.3)])) for i in range(10)), 10)
        self.assertEqual(sum(len(fast.update([person(40 * i, 0.9)])) for i in range(10)), 10)
        tracks = untracked([person(0, 0.3), person(100, 0.8)])
        self.assertEqual([t.id for t in tracks], [1, 2])

    def test_home_page_and_cameras_page(self):
        self.env()
        with mock.patch("simulation.camera.load_camera_configs", return_value={}),                 mock.patch("simulation.camera._enumerate_local", return_value=[]):
            home = self.client.get("/")
            self.assertEqual(self.client.get("/cameras/").status_code, 200)
        self.assertContains(home, "Getting started")
        self.assertContains(home, "2 of 2 camera(s) placed")



class DeviceTests(SimpleTestCase):
    def test_auto_prefers_nvidia_then_intel_then_cpu(self):
        from models.segmentation.yolo import detector as d
        cases = [(["cuda:0", "cuda:1"], ["intel:gpu"], "cuda:0", "yolo26s-seg"),
                 ([], ["intel:gpu.0", "intel:gpu.1"], "intel:gpu.0", "yolo26s-seg"),
                 ([], [], "cpu", "yolo11n-seg")]
        for cuda, intel, want, model in cases:
            with mock.patch.object(d, "cuda_gpus", return_value=cuda), mock.patch.object(d, "intel_gpus", return_value=intel):
                self.assertEqual(d.pick_device("auto"), want)
                self.assertEqual(d.default_variant(d.pick_device("auto")), model)
        self.assertEqual(d.pick_device("intel:gpu.1"), "intel:gpu.1")
