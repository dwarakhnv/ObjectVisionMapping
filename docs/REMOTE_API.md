# Remote access API

Other apps and services can read where objects are in an environment, live.
Keys are created on the **Remote access** page of the site (`/remote/`).

## Keys

- Each key belongs to **one environment** and can be limited to some object
  classes (for example only `person`). A key limited to people never returns
  anything else.
- The key is shown **once**, when it's created. Only a hash of it is stored.
  A lost key can't be recovered: revoke it and make a new one.
- Keys can be switched off and on, or revoked, from the same page.

Send the key in a header:

```
Authorization: Bearer ovm_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

`X-API-Key: <key>` also works, and so does `?token=<key>` in the URL. The URL
form is for browsers' `EventSource`, which can't send headers.

## Detection only runs while someone is connected

The first request starts detection on the environment's cameras. Detection keeps
running while requests keep coming (or a stream stays open) and **stops 30 s after
the last one**, so an environment nobody is connected to uses no compute.
Right after a pause, the first answers may show cameras as `"starting"` with no
objects. Poll at least every 30 s to keep detection running, or use the stream.

## Endpoints

Replace `<host>` with the address shown on the Remote access page (e.g.
`http://192.168.5.20:7080`) and `<env>` with the environment's id.

| Method and path | What it returns |
|---|---|
| `GET /api/v1/environments/<env>` | The environment: name, map kind, cameras, the classes this key can see |
| `GET /api/v1/environments/<env>/objects` | The objects right now (JSON) |
| `GET /api/v1/environments/<env>/stream` | Server-Sent Events: an `objects` event the moment the objects change, and a comment line every 15 s to keep the connection open |

Add `?classes=person,car` to narrow the results, within what the key allows.

## Getting results fast

Detection runs in the background, 8-9 times a second on a machine with an
Intel GPU. Each result is built once and handed to every client the moment it
exists. A typical object is about 0.1-0.2 s old when it arrives
(`age_ms`), most of it the detection itself.

- **Stream (fastest):** `/stream` sends each new result as it's ready, with
  no polling delay. Nothing is sent while nothing changes (for example, only
  parked cars).
- **Smooth stream:** `/stream?hz=20` sends up to 20 updates a second (1-30).
  Between results, moving objects are moved ahead along their heading and
  speed to the current moment, and marked `"predicted": true`. That is never
  more than 1 s ahead.
- **Long polling:** `/objects?after=<seq>&wait=10` answers as soon as there's
  a result newer than `seq` (from the previous answer), or after `wait`
  seconds (at most 25). Plain `/objects` answers at once.
- **Prediction when polling:** `/objects?predict=1` moves moving objects ahead
  to now, like `hz`.

Prediction needs the environment's **tracking** (Environments page, "Track
objects"). It is off by default; `tracking` in every answer says which.
Without tracking, `hz` and `predict` still work but simply repeat the latest
result.

### Errors

| Status | Meaning |
|---|---|
| 401 | No key sent |
| 403 | The key is wrong, revoked or switched off, is for another environment, or asks for classes it doesn't allow |
| 404 | No such environment |

## What you get

```json
{
  "environment": "front_street",
  "name": "409 Deercroft Dr. Front Street",
  "seq": 1824,
  "time": "2026-10-23T14:05:12.345+00:00",
  "captured_at": "2026-10-23T14:05:12.221+00:00", "age_ms": 124,
  "map": {"kind": "geo"},
  "cameras": {"tapo_c100": {"name": "Tapo C100", "status": "running", "error": null, "located": true}},
  "objects": [
    {
      "id": 3, "class": "person", "label": "Adult", "subtype": "adult", "confidence": 0.86,
      "position": {"lat": 35.6858123, "lon": -78.8780456},
      "heading": 92.0, "heading_source": "motion", "speed_mps": 1.3, "moving": true,
      "direction": "crossing_right",
      "size_m": {"length": 0.5, "width": 0.5, "height": 1.74},
      "cameras": ["tapo_c100"], "tracked_for_s": 8.4
    }
  ]
}
```

- **`position`:** outdoors it's `{lat, lon}`. Indoors (an environment on a floor
  plan) it's `{x, y, units}`, measured from the plan's top-left corner with
  y going down, and `map` gives the plan and its units.
- **`heading`:** degrees clockwise from north (indoors, from the top of the
  plan). It's `null` when unknown.
  - `heading_source` is `"motion"` for something moving.
  - It's `"shape"` for a parked vehicle, whose front can't be told apart from
    its back.
- **`id`:** with tracking on, stays the same while the object stays in view.
  With tracking off (the default) it only numbers the objects in this answer,
  and `speed_mps`, motion headings and `tracked_for_s` are `null`. Either way
  an object seen by two cameras appears once, listing both in `cameras`.
- **`tracking`:** whether the environment tracks objects (see above).
- **`seq`:** goes up with every new result. **`captured_at` / `age_ms`:**
  when the camera frame behind these objects was taken, and how long ago.
- **`predicted`:** only with `hz` or `predict=1`. `true` when the position was
  moved ahead; only moving objects with a heading from their motion are.
- **`cameras`, top level:** each camera's state: `starting`, `running`, or an
  error. `located` is false for a camera with fewer than 4 reference points;
  its objects have no position, so they aren't listed.

## Examples

Poll with curl:

```bash
curl -H "Authorization: Bearer $OVM_KEY" "http://<host>/api/v1/environments/<env>/objects"
```

Stream with Python:

```python
import json, requests

url = "http://<host>/api/v1/environments/<env>/stream"
with requests.get(url, headers={"Authorization": "Bearer " + KEY}, stream=True, timeout=60) as r:
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            for obj in json.loads(line[6:])["objects"]:
                print(obj["id"], obj["label"], obj["position"])
```

Smooth positions in Python, 20 times a second:

```python
url = "http://<host>/api/v1/environments/<env>/stream?hz=20&classes=person"
# ... same loop as above; objects with "predicted": true were moved ahead to now
```

Long polling, if a stream isn't possible:

```python
seq = 0
while True:
    data = requests.get(f"http://<host>/api/v1/environments/<env>/objects?after={seq}&wait=10",
                        headers={"Authorization": "Bearer " + KEY}, timeout=30).json()
    seq = data["seq"]
    print(data["age_ms"], "ms old:", [(o["id"], o["position"]) for o in data["objects"]])
```

Stream in a browser:

```js
const source = new EventSource("http://<host>/api/v1/environments/<env>/stream?token=" + KEY);
source.addEventListener("objects", (e) => console.log(JSON.parse(e.data).objects));
```

## Reaching it from other machines

- **Address:** the server listens on all network interfaces (`0.0.0.0:7080`).
  Other computers on the same network use this machine's address, which the
  Remote access page lists.
- **Firewall:** Windows may ask to allow Python through the firewall the first
  time.
- **Outside the network:** use a VPN or a tunnel rather than opening the port
  to the internet.
- **Production:** the development server runs over plain HTTP. For anything
  beyond a trusted network, put it behind HTTPS (a reverse proxy) so keys
  aren't sent in the clear.
