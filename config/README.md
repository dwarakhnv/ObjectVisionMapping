# config

Keys and secrets. Only `secrets.example.json` is shared; copy it to `secrets.json` and fill it in.

| Key | Used for |
|---|---|
| `django_secret_key` | Signing (CSRF). A random one is generated per start if missing. |
| `allowed_hosts` | Host names the site answers to (default: any). |
| `cameras.<name>` | Values (e.g. `username`, `password`) filled into a camera stream URL whose *Secret name* is `<name>`. |
