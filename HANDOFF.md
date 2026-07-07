# Handoff: verify the USB-hub contention fix on real hardware

**To continue this work on a box-connected computer:** open Claude Code in this repo
and say *"Read HANDOFF.md and follow it."* (Everything below is the full context.)

---

I'm continuing work on the Lager hardware-test repo (CLI + box services). On branch
`de/ykush-lager-group-selfheal` (already pushed to origin) I fixed a USB-hub device-
contention bug and need to VERIFY IT ON REAL HARDWARE — a box that has BOTH an Acroname
hub and a YKUSH hub attached. You're already on the branch if you checked it out; if not:
`git fetch origin && git checkout de/ykush-lager-group-selfheal`.

## The bug
`lager python <test>` failed with `OSError: open failed` when driving a USB hub net,
while `lager usb ...` still worked. Root cause: libusb access to these hubs is EXCLUSIVE,
and the driver cached the open handle indefinitely at class scope, so the long-lived
box_http_server process pinned the hub open — any separate process (each `lager python`
test runs in its own subprocess) then couldn't open it. `lager update` only "fixed" it by
restarting the container.

## The fix (box-side drivers)
- `box/lager/automation/usb_hub/usb_net.py`: new `hub_access(key, timeout)` = in-process
  `threading.Lock` + cross-process `util/device_lock.py` flock, keyed on the physical hub.
- `ykush.py` and `acroname.py`: release the handle after EVERY op
  (open->operate->release, no caching) and wrap the cycle in `hub_access`.
- Unit tests: `test/unit/box/test_ykush_driver.py` + `test_acroname_driver.py` (both pass;
  YKUSH logic validated, Acroname only MOCKED — this hardware run is the real Acroname
  check). Repro script: `test/api/usb/test_usb_contention.py` (edit its `NET` constant).

## Your task
Deploy the branch to a box with both hubs and verify end-to-end.

### Deploy (box `~/box` is a git checkout of lagerdata/lager)
- Clean: `cd ~/box && git fetch origin de/ykush-lager-group-selfheal && git checkout -f de/ykush-lager-group-selfheal && ./start_box.sh`  (use `./start_box.sh`, NOT `lager update`)
- Or overlay: scp `usb_net.py`/`ykush.py`/`acroname.py` into `~/box/lager/automation/usb_hub/` then `cd ~/box && ./start_box.sh`.
- Revert when done: `cd ~/box && git checkout -- lager/automation/usb_hub/ && ./start_box.sh`
- Confirm live: `docker exec lager grep -c hub_access /app/lager/lager/automation/usb_hub/ykush.py`

### Configure a USB net per hub
`lager nets --box <BOX>` (confirm both hubs appear), then `lager nets tui --box <BOX>` to
assign a net to a YKUSH port and one to an Acroname port.

### Test (for EACH hub — set `NET` in `test_usb_contention.py` to that hub's net)
1. `lager usb <NET> enable --box <BOX>` (makes box_http_server touch the hub), then
   `lager python test/api/usb/test_usb_contention.py --box <BOX>` — must print PASS with
   NO "open failed". Before the fix this exact sequence fails.
2. At idle: `docker exec lager sh -c 'ls -l /proc/*/fd 2>/dev/null | grep bus/usb'` should
   be EMPTY (nothing pins the hub between ops).
3. Concurrent stress: run `for i in $(seq 30); do lager usb <OTHER_NET> toggle --box <BOX>; done &`
   in the background while running the python script — all succeed, no "open failed".

## Cautions
- Do NOT test on JUL-8 (Akbar's active D4 bench, v0.31.0 — deploying/toggling there
  disrupts him and downgrades the box). Use a box you control.
- For Acroname specifically, watch that connect->op->disconnect PER CALL is reliable and
  not noticeably slow — flag it if so.
- Report any FAIL / "open failed" output.

**Note:** this branch also carries a separate lager-group/udev permission self-heal fix
(`start_box.sh`, cli deploy scripts) — unrelated to this contention issue; ignore it here.
Delete this HANDOFF.md before opening a PR.
