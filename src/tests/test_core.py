"""Core tests: geometry, coordinates, Environment storage and API, tracking, estimates, fusion.

Run from src/:  ..\\.venv\\Scripts\\python manage.py test tests
Nothing here loads the detection model or touches data/ (environments go to a temp folder).
"""
import json
import math
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from django.test import SimpleTestCase, override_settings

from environments import geometry
from environments.environment import CoordinateSystem, Environment, EnvironmentError
from mapping.fusion import Fusion
from models.objects.estimate import ObjectEstimator
from models.objects.registry import ObjectRegistry
from models.segmentation.base import Detection
from models.tracking.sort.tracker import Tracker, iou_matrix


def demo_env_dict(kind="local"):
    pts = [("A", -8, 10), ("B", 8, 10), ("C", -10, 22), ("D", 10, 22), ("E", 0, 18), ("F", -6, 34), ("G", 8, 36)]
    cs = {"kind": "local", "units": "ft", "meters_per_unit": 0.3048} if kind == "local" else \
        {"kind": "geo", "origin": {"lat": 40.7128, "lon": -74.006, "alt": 10.0}}
    return {"name": "Test site", "coordinates": cs, "extent": 60,
            "reference_points": [{"id": i, "name": i, "position": {"x": x, "y": y, "z": 0}} for i, x, y in pts],
            "cameras": [{"id": "cam1", "name": "Cam 1", "position": {"x": 0, "y": -2, "z": 0}, "height": 20,
                         "heading": 0, "tilt": 15, "hfov": 70, "resolution": [1280, 720],
                         "source": {"kind": "file", "uri": "x.mp4"}}]}


class GeometryTests(SimpleTestCase):
    def test_rotation_round_trip(self):
        for h, t, r in [(0, 10, 0), (45, 30, 5), (225, 12.5, -3), (359, 80, 0)]:
            got = geometry.angles_from_rotation(geometry.rotation(h, t, r))
            self.assertAlmostEqual((got[0] - h + 180) % 360 - 180, 0, places=6)
            self.assertAlmostEqual(got[1], t, places=6)
            self.assertAlmostEqual(got[2], r, places=6)

    def test_heading_zero_looks_north_and_tilt_looks_down(self):
        R = geometry.rotation(0, 30)
        forward = R[2]
        self.assertAlmostEqual(forward[0], 0)
        self.assertGreater(forward[1], 0)
        self.assertLess(forward[2], 0)

    def test_project_and_ground_point_invert(self):
        cam = geometry.CameraModel([3, -4, 8], 20, 25, 2, 65, 1920, 1080)
        P = np.array([[5.0, 20.0, 0.0]])
        (u, v), = cam.project(P)[0]
        np.testing.assert_allclose(cam.ground_point(u, v), P[0], atol=1e-6)

    def test_points_behind_camera_are_flagged(self):
        cam = geometry.CameraModel([0, 0, 5], 0, 10, 0, 60, 640, 480)
        _, front = cam.project(np.array([[0, -10, 0], [0, 10, 0]]))
        self.assertEqual(front.tolist(), [False, True])

    def test_scaled_camera_keeps_normalised_pixels(self):
        cam = geometry.CameraModel([0, 0, 6], 10, 15, 0, 70, 1920, 1080)
        small = cam.scaled(1280, 720)
        P = np.array([[2.0, 25.0, 0.0]])
        a, b = cam.project(P)[0][0], small.project(P)[0][0]
        np.testing.assert_allclose(a / [1920, 1080], b / [1280, 720], atol=1e-9)

    def test_solve_pose_recovers_camera(self):
        cam = geometry.CameraModel([1, -3, 7], 33, 18, 1.5, 62, 1280, 720)
        W = np.random.RandomState(0).uniform([-15, 8, 0], [15, 45, 0], (8, 3))
        pix, _ = cam.project(W)
        r = geometry.solve_pose(W, pix, cam.K)
        np.testing.assert_allclose(r["C"], cam.C, atol=1e-6)
        h, t, ro = geometry.angles_from_rotation(r["R"])
        self.assertAlmostEqual(h, 33, places=5)
        self.assertAlmostEqual(t, 18, places=5)
        self.assertLess(r["rmse"], 1e-6)

    def test_solve_pose_and_fov(self):
        cam = geometry.CameraModel([0, 0, 9], 350, 20, 0, 58, 1280, 720)
        W = np.random.RandomState(1).uniform([-15, 10, 0], [15, 50, 0], (10, 3))
        pix, _ = cam.project(W)
        r = geometry.solve_pose_and_fov(W, pix, 1280, 720, hfov_guess=70)
        self.assertAlmostEqual(r["hfov"], 58, places=2)
        np.testing.assert_allclose(r["C"], cam.C, atol=1e-3)

    def test_too_few_points(self):
        with self.assertRaises(ValueError):
            geometry.solve_pose(np.zeros((3, 3)), np.zeros((3, 2)), np.eye(3))


class CoordinateTests(SimpleTestCase):
    def test_local_units(self):
        cs = CoordinateSystem.from_dict({"kind": "local", "units": "ft", "meters_per_unit": 0.3048})
        np.testing.assert_allclose(cs.to_world({"x": 10, "y": 20, "z": 1}), [3.048, 6.096, 0.3048])
        self.assertAlmostEqual(cs.from_world(np.array([3.048, 6.096, 0.3048]))["y"], 20)

    def test_geo_round_trip_and_scale(self):
        cs = CoordinateSystem.from_dict({"kind": "geo", "origin": {"lat": 51.5, "lon": -0.12, "alt": 30}})
        p = {"lat": 51.5009, "lon": -0.1185, "alt": 35}
        w = cs.to_world(p)
        # ~0.0009 deg of latitude is ~100 m north; altitude difference is "up".
        self.assertAlmostEqual(w[1], 100.1, delta=0.5)
        self.assertAlmostEqual(w[2], 5.0, delta=0.05)
        back = cs.from_world(w)
        self.assertAlmostEqual(back["lat"], p["lat"], places=9)
        self.assertAlmostEqual(back["lon"], p["lon"], places=9)
        self.assertAlmostEqual(back["alt"], p["alt"], places=4)

    def test_validation(self):
        with self.assertRaises(EnvironmentError):
            CoordinateSystem.from_dict({"kind": "geo", "origin": {"lat": 95, "lon": 0}})
        with self.assertRaises(EnvironmentError):
            CoordinateSystem.from_dict({"kind": "local", "meters_per_unit": 0})


class EnvironmentTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_save_load_round_trip(self):
        env = Environment.from_dict(demo_env_dict())
        env.save(self.root)
        self.assertTrue((self.root / "test_site" / "environment.json").exists())
        again = Environment.load(self.root, "test_site")
        self.assertEqual(again.to_dict()["reference_points"], env.to_dict()["reference_points"])
        self.assertEqual(again.camera("cam1").hfov, 70)
        self.assertEqual([e.slug for e in Environment.list(self.root)], ["test_site"])

    def test_duplicate_ids_rejected(self):
        d = demo_env_dict()
        d["reference_points"].append(dict(d["reference_points"][0]))
        with self.assertRaises(EnvironmentError):
            Environment.from_dict(d)

    def test_bad_slug_cannot_escape_folder(self):
        with self.assertRaises(FileNotFoundError):
            Environment.load(self.root, "../etc")

    def test_camera_world_position_uses_units(self):
        env = Environment.from_dict(demo_env_dict())
        np.testing.assert_allclose(env.camera("cam1").world_position(env.coordinates), [0, -2 * 0.3048, 20 * 0.3048])

    def test_calibration_solves_pose_and_is_resolution_independent(self):
        env = Environment.from_dict(demo_env_dict())
        cs = env.coordinates
        true = geometry.CameraModel([0.5, -1.0, 6.5], 4, 13, 0, 70, 1280, 720)
        pts = [cs.to_world(p.position) for p in env.reference_points]
        pix, _ = true.project(np.array(pts))
        # Clicked on a 1920x1080 frame: stored as fractions, so the same pose comes out.
        env.camera("cam1").calibration.points = []
        from environments.environment import Correspondence
        for p, (u, v) in zip(env.reference_points, pix):
            env.camera("cam1").calibration.points.append(Correspondence(p.id, u / 1280, v / 720))
        env.camera("cam1").calibration.resolution = [1920, 1080]
        report = env.solve_camera("cam1")
        cam = env.camera("cam1")
        self.assertLess(report["rmse_px"], 1e-3)
        self.assertAlmostEqual(cam.heading, 4, places=3)
        self.assertAlmostEqual(cam.tilt, 13, places=3)
        self.assertAlmostEqual(cs.height_to_m(cam.height), 6.5, places=3)
        # Reprojection at any resolution lands on the same fractions.
        rp = env.reprojection("cam1")
        self.assertAlmostEqual(rp[0]["u"], pix[0][0] / 1280, places=4)

    def test_geo_environment_calibrates(self):
        d = demo_env_dict("geo")
        cs = CoordinateSystem.from_dict(d["coordinates"])
        d["reference_points"] = [{"id": p["id"], "name": p["id"],
                                  "position": cs.from_world(np.array([p["position"]["x"], p["position"]["y"], 0.0]))}
                                 for p in d["reference_points"]]
        d["cameras"][0]["position"] = cs.from_world(np.array([0.0, -2.0, 0.0]))
        d["cameras"][0]["height"] = 5
        env = Environment.from_dict(d)
        true = geometry.CameraModel([0, -2, 6], 0, 14, 0, 70, 1280, 720)
        pix, _ = true.project(np.array([cs.to_world(p.position) for p in env.reference_points]))
        from environments.environment import Correspondence
        env.camera("cam1").calibration.points = [Correspondence(p.id, u / 1280, v / 720) for p, (u, v) in zip(env.reference_points, pix)]
        env.solve_camera("cam1")
        cam = env.camera("cam1")
        self.assertAlmostEqual(cam.height, 6, places=2)
        self.assertAlmostEqual(cam.tilt, 14, places=2)


class ApiTests(SimpleTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.patches = [mock.patch("environments.views.ROOT", self.root), mock.patch("mapping.views.ROOT", self.root)]
        for p in self.patches:
            p.start()
        self.settings_override = override_settings(ENVIRONMENTS_DIR=self.root)
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def post(self, url, body):
        return self.client.post(url, json.dumps(body), content_type="application/json")

    def test_create_view_update_delete(self):
        r = self.post("/api/environments", demo_env_dict())
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["environment"]["slug"], "test_site")
        self.assertEqual(self.post("/api/environments", demo_env_dict()).status_code, 409)
        self.assertEqual(self.client.get("/legacy/environments/test_site/").status_code, 200)
        self.assertEqual(self.client.get("/legacy/environments/").status_code, 200)
        env = self.client.get("/api/environments/test_site").json()["environment"]
        env["cameras"][0]["heading"] = 90
        r = self.client.put("/api/environments/test_site", json.dumps(env), content_type="application/json")
        self.assertEqual(r.json()["environment"]["cameras"][0]["heading"], 90)
        bad = dict(env, coordinates={"kind": "nope"})
        r = self.client.put("/api/environments/test_site", json.dumps(bad), content_type="application/json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.delete("/api/environments/test_site").status_code, 200)
        self.assertEqual(self.client.get("/api/environments/test_site").status_code, 404)

    def test_reserved_names(self):
        d = demo_env_dict()
        d["name"] = "Preview"
        self.assertEqual(self.post("/api/environments", d).status_code, 400)

    def test_preview_and_convert(self):
        r = self.post("/api/environments/preview", demo_env_dict())
        self.assertEqual(len(r.json()["world"]["cameras"]), 1)
        r = self.post("/api/environments/convert", {"coordinates": {"kind": "local", "meters_per_unit": 2}, "world": [4, 6, 0]})
        self.assertEqual(r.json()["position"], {"x": 2.0, "y": 3.0, "z": 0.0})

    def test_calibration_api(self):
        self.post("/api/environments", demo_env_dict())
        env = Environment.load(self.root, "test_site")
        true = geometry.CameraModel([0, -0.6, 6], 2, 38, 0, 70, 1280, 720)
        pix, _ = true.project(np.array([env.coordinates.to_world(p.position) for p in env.reference_points]))
        pts = [{"point_id": p.id, "u": u / 1280, "v": v / 720} for p, (u, v) in zip(env.reference_points, pix)
               if 0 < u < 1280 and 0 < v < 720]
        self.assertGreaterEqual(len(pts), 6)
        r = self.post("/api/mapping/test_site/cameras/cam1/solve", {"points": pts[:3], "resolution": [1280, 720]})
        self.assertEqual(r.status_code, 400)  # needs 4
        r = self.post("/api/mapping/test_site/cameras/cam1/solve", {"points": pts, "resolution": [1280, 720], "estimate_fov": True})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertLess(r.json()["report"]["rmse_px"], 0.01)
        self.assertAlmostEqual(r.json()["camera"]["hfov"], 70, places=1)
        self.assertTrue(r.json()["overlay"]["grid"])
        self.assertEqual(self.client.get("/mapping/test_site/").status_code, 200)
        self.assertEqual(self.client.get("/mapping/test_site/live/").status_code, 200)
        r = self.client.put("/api/mapping/test_site/cameras/cam1/points", json.dumps({"points": [{"point_id": "ZZ", "u": 0.5, "v": 0.5}]}),
                            content_type="application/json")
        self.assertEqual(r.status_code, 400)


def det(box, key="person", score=0.9):
    return Detection(label=key, score=score, box=tuple(box), class_key=key)


class TrackerTests(SimpleTestCase):
    def test_iou(self):
        m = iou_matrix([[0, 0, 10, 10]], [[0, 0, 10, 10], [5, 0, 15, 10], [20, 20, 30, 30]])
        np.testing.assert_allclose(m[0], [1, 1 / 3, 0], atol=1e-9)

    def test_ids_stay_with_objects(self):
        reg = ObjectRegistry.load()
        tr = Tracker(groups=reg.groups())
        ids = {}
        for f in range(30):
            a = det([100 + 5 * f, 100, 140 + 5 * f, 200])           # moving right
            b = det([500 - 4 * f, 120, 560 - 4 * f, 230], "car")   # moving left
            dets = [a, b] if f != 12 else [a]                      # b missed for one frame
            for t in tr.update(dets):
                ids.setdefault(t.class_key, set()).add(t.id)
        self.assertEqual({k: len(v) for k, v in ids.items()}, {"person": 1, "car": 1})

    def test_vehicle_label_flips_keep_id(self):
        tr = Tracker(groups=ObjectRegistry.load().groups())
        seen = set()
        for f in range(10):
            for t in tr.update([det([100 + f, 100, 300 + f, 200], "truck" if f % 3 == 0 else "car")]):
                seen.add(t.id)
        self.assertEqual(len(seen), 1)


class EstimateTests(SimpleTestCase):
    def setUp(self):
        self.reg = ObjectRegistry.load()
        self.est = ObjectEstimator(self.reg)
        self.cam = geometry.CameraModel([0, 0, 6], 0, 15, 0, 70, 1280, 720)

    def run_track(self, foot_xy, height, steps=12, dt=0.1, key="person"):
        tr = Tracker(groups=self.reg.groups())
        out = None
        for i in range(steps):
            x, y = foot_xy(i * dt)
            foot = np.array([x, y, 0.0])
            (u0, v0), (u1, v1) = self.cam.project(np.array([foot, foot + [0, 0, height]]))[0]
            hw = (v0 - v1) * 0.15
            for t in tr.update([det([u0 - hw, v1, u0 + hw, v0], key)]):
                out = self.est.estimate(t, i * dt, 1280, 720, self.cam, 0.0)
        return out

    def test_child_adult_and_position(self):
        adult = self.run_track(lambda t: (2 + 1.5 * t, 15), 1.8)
        child = self.run_track(lambda t: (2 + 1.5 * t, 15), 1.1)
        self.assertEqual(adult["subtype"], "adult")
        self.assertEqual(child["subtype"], "child")
        self.assertAlmostEqual(adult["dimensions"]["height"], 1.8, delta=0.05)
        self.assertAlmostEqual(adult["world"][1], 15, delta=0.5)

    def test_directions(self):
        self.assertEqual(self.run_track(lambda t: (-3 + 1.5 * t, 15), 1.7)["direction"], "crossing_right")
        self.assertEqual(self.run_track(lambda t: (3 - 1.5 * t, 15), 1.7)["direction"], "crossing_left")
        a = self.run_track(lambda t: (1, 25 - 2 * t), 1.7)
        self.assertEqual(a["direction"], "approaching")
        self.assertAlmostEqual(a["heading"], 180, delta=5)
        self.assertAlmostEqual(a["speed_mps"], 2, delta=0.2)
        self.assertEqual(self.run_track(lambda t: (1, 12 + 2 * t), 1.7)["direction"], "receding")
        self.assertEqual(self.run_track(lambda t: (1, 15), 1.7)["direction"], "stationary")

    def test_uncalibrated_distance_from_average_height(self):
        tr = Tracker()
        f = (1280 / 2) / math.tan(math.radians(30))
        h_px = f * 1.7 / 20  # an average person 20 m away
        for i in range(3):
            tracks = tr.update([det([600, 300, 640, 300 + h_px])])
        o = self.est.estimate(tracks[0], 0.2, 1280, 720, None, hfov=60)
        self.assertAlmostEqual(o["distance_m"], 20, delta=0.1)
        self.assertIsNone(o["world"])
        self.assertEqual(o["size_source"], "class average")


class FusionTests(SimpleTestCase):
    def obj(self, oid, x, y, cls="person", dist=10):
        return {"id": oid, "class": cls, "label": cls, "world": [x, y, 0], "score": 0.9, "distance_m": dist,
                "dimensions": {"length": 0.5, "width": 0.5, "height": 1.7}}

    def test_merges_across_cameras_and_keeps_ids(self):
        f = Fusion(ObjectRegistry.load())
        a = f.fuse({"c1": [self.obj(1, 5, 5), self.obj(2, 20, 5)], "c2": [self.obj(7, 5.4, 5.3)]})
        self.assertEqual(len(a), 2)
        merged = [o for o in a if len(o["seen_by"]) == 2]
        self.assertEqual(len(merged), 1)
        b = f.fuse({"c1": [self.obj(1, 5.2, 5), self.obj(2, 20, 5.1)], "c2": [self.obj(7, 5.5, 5.2)]})
        self.assertEqual(sorted(o["id"] for o in a), sorted(o["id"] for o in b))

    def test_same_camera_and_other_class_not_merged(self):
        f = Fusion(ObjectRegistry.load())
        out = f.fuse({"c1": [self.obj(1, 5, 5), self.obj(2, 5.3, 5)], "c2": [self.obj(3, 5.1, 5, cls="car")]})
        self.assertEqual(len(out), 3)


class RegistryTests(SimpleTestCase):
    def test_custom_classes_load_from_json(self):
        d = Path(tempfile.mkdtemp())
        try:
            path = d / "classes.json"
            path.write_text(json.dumps({"classes": [{"key": "bicycle", "label": "Bicycle", "detector_labels": ["bicycle"],
                                                     "dimensions": {"length": 1.8, "width": 0.6, "height": 1.1}}]}))
            reg = ObjectRegistry.load(path)
            self.assertEqual(reg.for_label("Bicycle").key, "bicycle")
            self.assertIsNone(reg.for_label("car"))
        finally:
            shutil.rmtree(d)


class SourceTests(SimpleTestCase):
    def test_upload_lists_and_rejects_bad_types(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        tmp = Path(tempfile.mkdtemp())
        try:
            with override_settings(SOURCES_DIR=tmp, ENVIRONMENTS_DIR=tmp / "envs"),                     mock.patch("vision.feeds.GEO_ENV_DIR", tmp / "geo"):
                r = self.client.post("/api/sources", {"file": SimpleUploadedFile("my clip (1).mp4", b"00", "video/mp4")})
                self.assertEqual(r.status_code, 201)
                self.assertEqual(r.json()["name"], "my_clip_1_.mp4")
                r = self.client.post("/api/sources", {"file": SimpleUploadedFile("my clip (1).mp4", b"00", "video/mp4")})
                self.assertEqual(r.json()["name"], "my_clip_1__2.mp4")
                r = self.client.post("/api/sources", {"file": SimpleUploadedFile("evil.exe", b"00")})
                self.assertEqual(r.status_code, 400)
                feeds = self.client.get("/api/feeds").json()["feeds"]
                self.assertEqual(sorted(f["id"] for f in feeds), ["~my_clip_1_.mp4", "~my_clip_1__2.mp4"])
                from vision.sources import SourceError, resolve_file
                with self.assertRaises(SourceError):
                    resolve_file("../secrets.json")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)



class FakeRTSP:
    """Tiny RTSP server that wants Digest auth for user/pass and only has stream1."""

    def __init__(self, user="viewer", password="p@ss word"):
        import hashlib
        import socket
        import threading
        self.user, self.password = user, password
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.h = lambda s: hashlib.md5(s.encode()).hexdigest()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        import re
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                while True:
                    req = conn.recv(4096).decode()
                    if not req:
                        break
                    uri = req.split()[1]
                    m = re.search(r'username="([^"]*)".*nonce="([^"]*)".*response="([^"]*)"', req)
                    if not m:
                        conn.sendall(b'RTSP/1.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="TP-LINK IP-Camera", nonce="abc"\r\n\r\n')
                        continue
                    want = self.h("%s:abc:%s" % (self.h("%s:TP-LINK IP-Camera:%s" % (self.user, self.password)), self.h("DESCRIBE:" + uri)))
                    if m.group(1) != self.user or m.group(3) != want:
                        conn.sendall(b"RTSP/1.0 401 Unauthorized\r\n\r\n")
                    elif not uri.endswith("/stream1"):
                        conn.sendall(b"RTSP/1.0 404 Not Found\r\n\r\n")
                    else:
                        conn.sendall(b"RTSP/1.0 200 OK\r\n\r\n")

    def close(self):
        self.sock.close()


class CameraTests(SimpleTestCase):
    """RTSP camera code without hardware: config, URL building, and the connection check."""

    def test_config_and_url(self):
        from simulation.camera import CameraError, RTSPCamera
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "secrets.json"
            f.write_text(json.dumps({"django_secret_key": "x", "cameras": {
                "gate": {"username": "x"},  # no host: ignored
                "tapo": {"name": "Tapo", "host": "10.0.0.5", "username": "viewer", "password": "p@ss word"}}}))
            cam = RTSPCamera.from_config(path=f)
            self.assertEqual(cam.key, "tapo")
            self.assertEqual(cam.url, "rtsp://viewer:p%40ss%20word@10.0.0.5:554/stream1")
            self.assertNotIn("viewer", cam.safe_url)
            with self.assertRaises(CameraError):
                RTSPCamera.from_config("nope", path=f)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_check_explains_failures(self):
        import socket
        from simulation.camera import RTSPCamera
        srv = FakeRTSP()
        try:
            ok, msg = RTSPCamera("127.0.0.1", "viewer", "p@ss word", "stream1", srv.port).check()
            self.assertTrue(ok, msg)
            ok, msg = RTSPCamera("127.0.0.1", "viewer", "wrong", "stream1", srv.port).check()
            self.assertFalse(ok)
            self.assertIn("Camera Account", msg)
            ok, msg = RTSPCamera("127.0.0.1", "viewer", "p@ss word", "stream9", srv.port).check()
            self.assertIn("stream1 or stream2", msg)
        finally:
            srv.close()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        ok, msg = RTSPCamera("127.0.0.1", "u", "p", "stream1", free).check()
        self.assertFalse(ok)
        self.assertIn("refuses RTSP", msg)

    def test_home_page_and_api(self):
        from simulation import views
        with mock.patch("simulation.camera.load_camera_configs", return_value={}),                 mock.patch("simulation.camera._enumerate_local", return_value=[]):
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/api/cameras").json(), {"cameras": []})
            self.assertEqual(self.client.get("/api/cameras/nope").status_code, 404)


class FlipTests(SimpleTestCase):
    def test_flips_from_config_and_orientation(self):
        from simulation.camera import RTSPCamera
        frame = np.arange(6, dtype=np.uint8).reshape(2, 3)  # [[0 1 2] [3 4 5]]
        both = RTSPCamera("h", "u", "p", v_flipped=True, h_flipped=True)
        np.testing.assert_array_equal(both.orient(frame), [[5, 4, 3], [2, 1, 0]])         # rotated 180
        np.testing.assert_array_equal(RTSPCamera("h", "u", "p", v_flipped=True).orient(frame), [[3, 4, 5], [0, 1, 2]])
        np.testing.assert_array_equal(RTSPCamera("h", "u", "p", h_flipped=True).orient(frame), [[2, 1, 0], [5, 4, 3]])
        self.assertIs(RTSPCamera("h", "u", "p").orient(frame), frame)
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "secrets.json"
            f.write_text(json.dumps({"cameras": {"c": {"host": "h", "v_flipped": True, "h_flipped": True}}}))
            cam = RTSPCamera.from_config(path=f)
            self.assertTrue(cam.v_flipped and cam.h_flipped)
            self.assertTrue(cam.status()["v_flipped"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class GeoFixture:
    """A simulated camera and helpers for the simulation/environment.py tests."""

    LAT0, LON0 = 35.6858, -78.8780

    def setUp(self):
        from simulation.environment import Camera
        # A camera 6 m up looking north-east and down, 1920x1080; ground points in metres east/north.
        self.sim = geometry.CameraModel([0, 0, 6], 40, 25, 0, 70, 1920, 1080)
        self.cam = Camera(resolution=(1920, 1080), source="tapo_c100")

    def geo(self, east, north):
        from shapely.geometry import Point
        lat = self.LAT0 + math.degrees(north / 6371008.8)
        lon = self.LON0 + math.degrees(east / (6371008.8 * math.cos(math.radians(self.LAT0))))
        return Point(lon, lat)

    def add(self, *ground):
        from simulation.environment import ReferencePoint
        for e, n in ground:
            (u, v), = self.sim.project(np.array([[e, n, 0.0]]))[0]
            self.cam.add_reference_point(ReferencePoint((u, v), self.geo(e, n)))

    def dist_m(self, p, q):
        dy = math.radians(p.y - q.y) * 6371008.8
        dx = math.radians(p.x - q.x) * 6371008.8 * math.cos(math.radians(self.LAT0))
        return math.hypot(dx, dy)



class PixelToGeoTests(GeoFixture, SimpleTestCase):
    """simulation/environment.py: pixel -> geo mapping checked against a simulated camera."""

    def test_maps_any_ground_pixel_accurately(self):
        self.add((2, 12), (14, 8), (6, 30), (25, 22), (10, 18))
        for e, n in [(5, 15), (20, 25), (12, 9), (30, 35)]:
            (u, v), = self.sim.project(np.array([[e, n, 0.0]]))[0]
            self.assertLess(self.dist_m(self.cam.pixel_to_geo((u, v)), self.geo(e, n)), 0.01)
        self.assertLess(self.cam.accuracy(), 0.01)

    def test_other_resolution(self):
        self.add((2, 12), (14, 8), (6, 30), (25, 22))
        (u, v), = self.sim.project(np.array([[12.0, 20.0, 0.0]]))[0]
        a = self.cam.pixel_to_geo((u, v))
        b = self.cam.pixel_to_geo((u * 2 / 3, v * 2 / 3), frame_size=(1280, 720))
        self.assertLess(self.dist_m(a, b), 0.001)

    def test_bad_inputs(self):
        self.add((2, 12), (14, 8), (6, 30))
        with self.assertRaises(ValueError):          # only 3 points
            self.cam.pixel_to_geo((900, 600))
        self.cam.reference_points = []
        self.add((0, 10), (5, 15), (10, 20), (15, 25))  # all in one line
        self.cam._homography = None
        with self.assertRaises(ValueError):
            self.cam.pixel_to_geo((900, 600))

    def test_above_horizon_is_rejected(self):
        self.sim = geometry.CameraModel([0, 0, 6], 40, 8, 0, 70, 1920, 1080)  # tilted only 8 deg: sky at the top
        self.add((2, 12), (14, 8), (6, 30), (25, 22))
        with self.assertRaises(ValueError):
            self.cam.pixel_to_geo((960, 5))          # top of the image: sky

    def test_source_points_at_secrets(self):
        from simulation.environment import Camera
        with self.assertRaises(ValueError):
            Camera().connect()
        with mock.patch("simulation.camera.RTSPCamera.from_config", return_value="live") as fc:
            self.assertEqual(self.cam.connect(), "live")
            fc.assert_called_once_with("tapo_c100")


class GeoEnvironmentTests(GeoFixture, SimpleTestCase):
    """Step 2: saving/loading environments, pose estimation, and the environment API."""

    def test_save_and_load_round_trip(self):
        from simulation.environment import Environment
        self.cam.name = "Tapo C100"
        self.add((2, 12), (14, 8), (6, 30), (25, 22))
        env = Environment("Front street", [self.cam])
        tmp = Path(tempfile.mkdtemp())
        try:
            path = env.save(tmp)
            self.assertEqual(path.name, "front_street.json")
            again = Environment.load(tmp, "front_street")
            self.assertEqual(again.to_dict(), env.to_dict())
            cam = again.camera("tapo_c100")
            self.assertEqual(cam.source, "tapo_c100")
            p, q = cam.pixel_to_geo((900, 700)), self.cam.pixel_to_geo((900, 700))
            self.assertLess(self.dist_m(p, q), 1e-6)
            self.assertEqual([e.slug for e in Environment.list(tmp)], ["front_street"])
            with self.assertRaises(FileNotFoundError):
                Environment.load(tmp, "../secrets")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_invalid_data_is_rejected(self):
        from simulation.environment import Environment
        for bad in ({"name": ""}, {"name": "x", "cameras": [{"name": "a"}, {"name": "A"}]},
                    {"name": "x", "cameras": [{"name": "a", "reference_points": [{"pixel": [1, 2], "lat": 95, "lon": 0}]}]}):
            with self.assertRaises(ValueError):
                Environment.from_dict(bad)

    def test_estimate_pose(self):
        self.add((2, 12), (14, 8), (6, 30), (25, 22), (10, 18))
        pose = self.cam.estimate_pose()
        from shapely.geometry import Point
        self.assertLess(self.dist_m(self.geo(0, 0), Point(pose["lon"], pose["lat"])), 0.01)
        self.assertAlmostEqual(pose["height_m"], 6, places=2)
        self.assertAlmostEqual(pose["heading"], 40, places=1)
        self.assertAlmostEqual(pose["tilt"], 25, places=1)
        self.assertAlmostEqual(pose["hfov"], 70, places=1)

    def test_environment_api(self):
        from simulation import views
        tmp = Path(tempfile.mkdtemp())
        self.cam.name = "Tapo C100"
        self.add((2, 12), (14, 8), (6, 30), (25, 22))
        body = {"name": "Front street", "cameras": [self.cam.to_dict()]}
        try:
            with mock.patch.object(views, "ENV_DIR", tmp):
                r = self.client.post("/api/envs", json.dumps(body), content_type="application/json")
                self.assertEqual(r.status_code, 201, r.content)
                a = r.json()["analysis"]
                self.assertAlmostEqual(a["cameras"][0]["pose"]["height_m"], 6, places=1)
                self.assertEqual(len(a["world"]["reference_points"]), 4)
                self.assertEqual(self.client.post("/api/envs", json.dumps(body), content_type="application/json").status_code, 409)
                body["cameras"][0]["reference_points"].pop()  # 3 points: saved, but no pose
                r = self.client.put("/api/envs/front_street", json.dumps(body), content_type="application/json")
                self.assertIn("at least 4", r.json()["analysis"]["cameras"][0]["error"])
                body["name"] = "Back street"  # rename moves the file
                r = self.client.put("/api/envs/front_street", json.dumps(body), content_type="application/json")
                self.assertEqual(r.json()["slug"], "back_street")
                self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["back_street.json"])
                self.assertEqual(self.client.get("/environments/").status_code, 200)
                self.assertEqual(self.client.delete("/api/envs/back_street").status_code, 200)
                self.assertEqual(self.client.get("/api/envs/back_street").status_code, 404)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_detect_feed_gets_geo(self):
        """The Detect pipeline's camera model from the estimated pose agrees with pixel_to_geo."""
        from vision import feeds
        self.add((2, 12), (14, 8), (6, 30), (25, 22))
        model, pose = feeds.geo_camera_model(self.cam, 1280, 720)  # the stream is smaller than the reference image
        (u, v), = self.sim.project(np.array([[12.0, 20.0, 0.0]]))[0]
        u, v = u * 2 / 3, v * 2 / 3
        e, n, _ = model.ground_point(u, v)
        p = self.cam.pixel_to_geo((u, v), frame_size=(1280, 720))
        self.assertLess(self.dist_m(p, self.geo(12, 20)), 0.01)
        from simulation.environment import local_to_geo
        lat, lon = local_to_geo(e, n, self.cam._origin)
        from shapely.geometry import Point
        self.assertLess(self.dist_m(Point(lon, lat), self.geo(12, 20)), 0.05)


class SharedCameraTests(SimpleTestCase):
    def test_start_while_stopping_restarts_the_reader(self):
        """An idle stop racing with a new viewer must not leave the camera without a reader."""
        import threading
        import time as _time
        from simulation.camera import RTSPCamera
        cam = RTSPCamera("h", "u", "p")
        runs = []

        def fake_run():
            runs.append(threading.current_thread())
            while not cam._stop.is_set():
                _time.sleep(0.01)
            _time.sleep(0.2)  # slow to shut down, like a blocking cap.read()

        cam._run = fake_run
        cam.start()
        cam._stop.set()          # an idle stop has begun ...
        cam.start()              # ... and a new viewer arrives before the old reader has exited
        _time.sleep(0.1)
        self.assertEqual(len(runs), 2)
        self.assertTrue(cam.is_running())
        self.assertFalse(cam._stop.is_set())
        cam.stop()


class OrientationTests(SimpleTestCase):
    """models/objects/orientation.py and the heading logic, with a simulated camera."""

    def setUp(self):
        # 8 m up, looking north and down, like a camera across a street.
        self.cam = geometry.CameraModel([0, 0, 8], 0, 25, 0, 80, 1280, 720)
        self.ground = lambda px: np.array([self.cam.ground_point(u, v)[:2] for u, v in px])

    def outline(self, x, y, heading, L=4.5, W=1.8, H=1.5):
        """Image outline (convex hull) and box of a car-shaped cuboid at (x, y) facing `heading`."""
        h = math.radians(heading)
        fwd, right = np.array([math.sin(h), math.cos(h)]), np.array([math.cos(h), -math.sin(h)])
        corners = [np.r_[np.array([x, y]) + a * L / 2 * fwd + b * W / 2 * right, z]
                   for a in (-1, 1) for b in (-1, 1) for z in (0.0, H)]
        pix, _ = self.cam.project(np.array(corners))
        hull = cv2.convexHull(pix.astype(np.float32)).reshape(-1, 2)
        return [tuple(p) for p in hull], (*pix.min(axis=0), *pix.max(axis=0))

    def test_side_view_gives_the_length_axis(self):
        from models.objects.orientation import axis_distance, footprint_axis
        for heading in (90, 80, 100):  # parked along an east-west street
            poly, box = self.outline(2, 12, heading)
            axis, clear = footprint_axis(poly, box, self.ground, 4.5, 1.8)
            self.assertLess(axis_distance(axis, heading % 180), 8, (heading, axis))

    def test_end_view_gives_the_length_axis(self):
        from models.objects.orientation import axis_distance, footprint_axis
        poly, box = self.outline(0, 14, 0)  # facing straight away from the camera
        axis, clear = footprint_axis(poly, box, self.ground, 4.5, 1.8)
        self.assertLess(axis_distance(axis, 0), 10, axis)

    def test_street_grid(self):
        from models.objects.orientation import StreetGrid
        g = StreetGrid()
        self.assertIsNone(g.value)
        for a in [88, 91, 2, 179, 90, 1] * 3:  # along the street and across it
            g.add(a, 1.0)
        self.assertLess(min(abs(g.value - 0), abs(g.value - 90)), 2)
        self.assertAlmostEqual(g.snap(84) % 180, g.value + (90 if g.value < 45 else 0), delta=0.01)
        self.assertEqual(g.snap(45), 45)  # too far from either line: unchanged

    def run_car(self, path, seconds=3.0, fps=8, jitter=0.0, seed=0):
        from models.objects.estimate import ObjectEstimator
        reg = ObjectRegistry.load()
        est, tr = ObjectEstimator(reg), Tracker(groups=reg.groups())
        rng = np.random.RandomState(seed)
        out = None
        for i in range(int(seconds * fps)):
            t = i / fps
            x, y, heading = path(t)
            poly, box = self.outline(x + rng.normal(0, jitter), y + rng.normal(0, jitter), heading)
            d = Detection("car", 0.9, tuple(float(v) for v in box), poly, "car")
            for track in tr.update([d]):
                out = est.estimate(track, t, 1280, 720, self.cam, 0.0, ground_fn=self.ground)
        return out

    def test_parked_car_with_jitter_is_stationary_and_along_the_street(self):
        from models.objects.orientation import axis_distance
        o = self.run_car(lambda t: (3, 12, 90), jitter=0.25)
        self.assertEqual(o["direction"], "stationary")
        self.assertEqual(o["speed_mps"], 0.0)
        self.assertEqual(o["heading_source"], "shape")
        self.assertLess(axis_distance(o["heading"] % 180, 90), 8)

    def test_moving_car_gets_its_front_from_motion(self):
        from models.objects.orientation import heading_distance
        o = self.run_car(lambda t: (-8 + 5 * t, 13, 90))  # driving east at 5 m/s
        self.assertEqual(o["heading_source"], "motion")
        self.assertTrue(o["heading_confident"])
        self.assertLess(heading_distance(o["heading"], 90), 8)
        o = self.run_car(lambda t: (8 - 5 * t, 13, 270))  # driving west
        self.assertLess(heading_distance(o["heading"], 270), 8)
