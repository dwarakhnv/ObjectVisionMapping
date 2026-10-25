"""Camera geometry: poses, projection, back-projection and pose solving.

Conventions
-----------
World frame: local East-North-Up in metres (x east, y north, z up).
Camera frame: OpenCV (x right, y down, z forward).
Camera orientation is given the way people describe a mounted camera:
  heading  compass direction it points, degrees clockwise from north (0-360)
  tilt     degrees below the horizon (positive = looking down)
  roll     degrees the image is rotated clockwise, as seen from behind the camera
Intrinsics come from the field of view and the image resolution (pinhole,
square pixels unless a vertical field of view is given, no lens distortion).
"""
import math

import numpy as np


def rotation(heading_deg, tilt_deg, roll_deg=0.0):
    """World -> camera rotation matrix (rows are the camera's right, down and forward axes)."""
    h, t, r = (math.radians(a) for a in (heading_deg, tilt_deg, roll_deg))
    forward = np.array([math.sin(h) * math.cos(t), math.cos(h) * math.cos(t), -math.sin(t)])
    right0 = np.array([math.cos(h), -math.sin(h), 0.0])
    down0 = np.cross(forward, right0)
    right = right0 * math.cos(r) + down0 * math.sin(r)
    down = -right0 * math.sin(r) + down0 * math.cos(r)
    return np.vstack([right, down, forward])


def angles_from_rotation(R):
    """Inverse of rotation(): (heading, tilt, roll) in degrees."""
    right, forward = R[0], R[2]
    heading = math.degrees(math.atan2(forward[0], forward[1])) % 360.0
    tilt = math.degrees(-math.asin(max(-1.0, min(1.0, forward[2]))))
    h = math.radians(heading)
    right0 = np.array([math.cos(h), -math.sin(h), 0.0])
    down0 = np.cross(forward, right0)
    roll = math.degrees(math.atan2(float(right @ down0), float(right @ right0)))
    return heading, tilt, roll


def intrinsics(hfov_deg, width, height, vfov_deg=None):
    """3x3 camera matrix from the field of view (degrees) and resolution (pixels)."""
    fx = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    fy = (height / 2.0) / math.tan(math.radians(vfov_deg) / 2.0) if vfov_deg else fx
    return np.array([[fx, 0.0, width / 2.0], [0.0, fy, height / 2.0], [0.0, 0.0, 1.0]])


def vertical_fov(hfov_deg, width, height):
    """Vertical field of view for square pixels."""
    return math.degrees(2 * math.atan(math.tan(math.radians(hfov_deg) / 2.0) * height / width))


class CameraModel:
    """A posed pinhole camera in the world frame (metres)."""

    def __init__(self, position, heading, tilt, roll, hfov, width, height, vfov=None):
        self.C = np.asarray(position, dtype=float)  # camera centre (x, y, z) in metres
        self.R = rotation(heading, tilt, roll)
        self.K = intrinsics(hfov, width, height, vfov)
        self.width, self.height = int(width), int(height)

    def project(self, points):
        """World points (N,3) -> pixels (N,2) and a mask of points in front of the camera."""
        P = np.atleast_2d(np.asarray(points, dtype=float))
        cam = (P - self.C) @ self.R.T
        in_front = cam[:, 2] > 1e-6
        z = np.where(in_front, cam[:, 2], 1.0)
        pix = (cam[:, :2] / z[:, None]) * np.array([self.K[0, 0], self.K[1, 1]]) + self.K[:2, 2]
        return pix, in_front

    def ray(self, u, v):
        """Unit direction (world) of the ray through pixel (u, v)."""
        d_cam = np.array([(u - self.K[0, 2]) / self.K[0, 0], (v - self.K[1, 2]) / self.K[1, 1], 1.0])
        d = self.R.T @ d_cam
        return d / np.linalg.norm(d)

    def ground_point(self, u, v, ground_z=0.0, max_range=5000.0):
        """Where the ray through (u, v) meets the plane z = ground_z, or None."""
        d = self.ray(u, v)
        if d[2] >= -1e-9:
            return None  # at or above the horizon
        t = (ground_z - self.C[2]) / d[2]
        if t <= 0 or t > max_range:
            return None
        return self.C + t * d

    def scaled(self, width, height):
        """The same camera at another resolution (same field of view)."""
        clone = CameraModel.__new__(CameraModel)
        clone.C, clone.R = self.C.copy(), self.R.copy()
        sx, sy = width / self.width, height / self.height
        clone.K = self.K.copy()
        clone.K[0, 0] *= sx
        clone.K[0, 2] *= sx
        clone.K[1, 1] *= sy
        clone.K[1, 2] *= sy
        clone.width, clone.height = int(width), int(height)
        return clone


def solve_pose(world_points, pixels, K, initial=None):
    """Camera pose from 2D-3D correspondences (needs >= 4).

    Returns {"R", "C", "rmse", "residuals"} or raises ValueError. Uses OpenCV's
    solvePnP; `initial` (R, C) seeds the iterative solver when given.
    """
    import cv2

    W = np.asarray(world_points, dtype=np.float64).reshape(-1, 3)
    p = np.asarray(pixels, dtype=np.float64).reshape(-1, 2)
    if len(W) < 4:
        raise ValueError("At least 4 reference points are needed to solve a camera pose.")
    # Re-centre the world points: better conditioned for large coordinates.
    centre = W.mean(axis=0)
    Wc = W - centre
    if initial is not None:
        R0, C0 = initial
        rvec0, _ = cv2.Rodrigues(np.asarray(R0, dtype=np.float64))
        tvec0 = -np.asarray(R0) @ (np.asarray(C0) - centre)
        ok, rvec, tvec = cv2.solvePnP(Wc, p, K, None, rvec0, tvec0.reshape(3, 1), True, cv2.SOLVEPNP_ITERATIVE)
    else:
        ok, rvec, tvec = cv2.solvePnP(Wc, p, K, None, flags=cv2.SOLVEPNP_SQPNP)
        if ok:
            ok, rvec, tvec = cv2.solvePnP(Wc, p, K, None, rvec, tvec, True, cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        raise ValueError("Could not solve the camera pose from these points.")
    R, _ = cv2.Rodrigues(rvec)
    C = (-R.T @ tvec).ravel() + centre
    proj, _ = cv2.projectPoints(Wc, rvec, tvec, K, None)
    residuals = np.linalg.norm(proj.reshape(-1, 2) - p, axis=1)
    return {"R": R, "C": C, "rmse": float(np.sqrt(np.mean(residuals ** 2))), "residuals": residuals.tolist()}


def solve_pose_and_fov(world_points, pixels, width, height, hfov_guess):
    """Pose plus horizontal field of view (needs >= 6 points spread over the image)."""
    import cv2

    W = np.asarray(world_points, dtype=np.float64).reshape(-1, 3)
    p = np.asarray(pixels, dtype=np.float64).reshape(-1, 2)
    if len(W) < 6:
        raise ValueError("At least 6 reference points are needed to estimate the field of view.")
    centre = W.mean(axis=0)
    K0 = intrinsics(hfov_guess, width, height)
    flags = (cv2.CALIB_USE_INTRINSIC_GUESS | cv2.CALIB_FIX_PRINCIPAL_POINT | cv2.CALIB_FIX_ASPECT_RATIO
             | cv2.CALIB_ZERO_TANGENT_DIST | cv2.CALIB_FIX_K1 | cv2.CALIB_FIX_K2 | cv2.CALIB_FIX_K3)
    # calibrateCamera needs an intrinsic guess for non-planar points; we always pass one.
    _rms, K, _dist, rvecs, tvecs = cv2.calibrateCamera(
        [(W - centre).astype(np.float32)], [p.astype(np.float32)], (int(width), int(height)), K0, None, flags=flags)
    R, _ = cv2.Rodrigues(rvecs[0])
    C = (-R.T @ tvecs[0]).ravel() + centre
    hfov = math.degrees(2 * math.atan((width / 2.0) / K[0, 0]))
    proj, _ = cv2.projectPoints((W - centre), rvecs[0], tvecs[0], K, None)
    residuals = np.linalg.norm(proj.reshape(-1, 2) - p, axis=1)
    return {"R": R, "C": C, "K": K, "hfov": hfov, "rmse": float(np.sqrt(np.mean(residuals ** 2))),
            "residuals": residuals.tolist()}
