"""Django settings for ObjectVisionMapping.

Paths: code lives in src/, data in ../data, keys and secrets in ../config.
Secrets come from config/secrets.json (never committed; see secrets.example.json).
"""
import json
import os
import secrets as _secrets
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SRC_DIR.parent
DATA_DIR = Path(os.environ.get("OVM_DATA_DIR", PROJECT_DIR / "data"))
CONFIG_DIR = Path(os.environ.get("OVM_CONFIG_DIR", PROJECT_DIR / "config"))
ENVIRONMENTS_DIR = DATA_DIR / "environments"
MODELS_DIR = DATA_DIR / "models"
SOURCES_DIR = DATA_DIR / "sources"
OBJECT_CLASSES_FILE = DATA_DIR / "object_classes.json"


def _load_secrets():
    path = CONFIG_DIR / "secrets.json"
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


SECRETS = _load_secrets()
SECRET_KEY = SECRETS.get("django_secret_key") or os.environ.get("OVM_SECRET_KEY") or _secrets.token_urlsafe(50)
DEBUG = os.environ.get("OVM_DEBUG", "1") == "1"
ALLOWED_HOSTS = SECRETS.get("allowed_hosts") or ["*"]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "simulation",
    "remote",
    "environments",
    "vision",
    "mapping",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "ovm.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [SRC_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": ["django.template.context_processors.request", "ovm.context.asset_version"]},
}]
WSGI_APPLICATION = "ovm.wsgi.application"
DATABASES = {}  # everything is stored as JSON files under data/

STATIC_URL = "/static/"
STATICFILES_DIRS = [SRC_DIR / "static"]
DATA_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024
MAX_SOURCE_UPLOAD_BYTES = 1024 * 1024 * 1024
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Vision pipeline
# Detector: empty picks the best that runs fast here (yolo26s-seg on an Intel GPU via OpenVINO, else yolo11n-seg).
OVM_DETECTOR = os.environ.get("OVM_DETECTOR", "")
OVM_DEVICE = os.environ.get("OVM_DEVICE", "auto")  # auto, intel:gpu, intel:cpu (OpenVINO), cpu, cuda:0 (PyTorch)
OVM_IMGSZ = int(os.environ.get("OVM_IMGSZ", "960"))  # inference size: larger finds smaller, farther objects but is slower
OVM_MAX_FPS = float(os.environ.get("OVM_MAX_FPS", "15"))
OVM_FEED_IDLE_SECONDS = float(os.environ.get("OVM_FEED_IDLE_SECONDS", "30"))
