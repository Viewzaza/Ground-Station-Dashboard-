# Commissioning probes

Read-only checks against the station hardware. Nothing here moves the antenna,
changes a camera setting, or writes anything: `dump_caps` is answered from
rotctld's compiled-in capability struct without touching the serial line, and
the camera scripts only GET from ISAPI. Both are safe to run during a pass.

Run them when the rack is rebuilt, a device is replaced, or the dashboard
starts disagreeing with reality.

```bash
backend/.venv/Scripts/python deploy/commissioning/01_rotator_port.py 10.90.36.140
backend/.venv/Scripts/python deploy/commissioning/02_camera_codec.py 10.90.36.130 admin
```

The camera script reads `deploy/secrets/camera_password.txt` if it exists and
otherwise prompts, so the password stays out of shell history.

## What station 5024 answered, 2026-09-13

```
4532  closed — there is no rigctld
4533  SPID Rot2Prog, model 901, Az-El
      az -180..540, el -20..210, 600 baud 8N1, 300 ms post-write delay
      Can set Position: Y   Can Stop: Y   Can Park: N   Can Move: N
      get_pos -> RPRT -5    (controller powered down at the time)

camera  Hikvision DS-2CD1023G2-LIUF/SL "INSTED-GS_1", firmware V5.8.4
        101  1920x1080  H.264  8192 kbps
        102  640x360    H.264  1024 kbps
        RTSP: profile-level-id=420029 (Baseline 4.1), packetization-mode=1
```

Three of those lines changed code, and are the reason to re-run these rather
than trust the table:

- **`Can Park: N`** — park is a `set_pos` to the configured park coordinates,
  not a park command. A park command returns an error and the antenna does not
  move.
- **One camera, two channels** — the tiles are two views of one device, so it
  failing blanks both. They are labelled `main · 1080p` and `sub · 360p`
  rather than "Camera 1" and "Camera 2".
- **`RPRT -5` with capabilities still readable** — rotctld answers `dump_caps`
  from compiled data whether or not the controller is powered. Capabilities
  being readable is not evidence that the rotator is alive; only `get_pos` is.
