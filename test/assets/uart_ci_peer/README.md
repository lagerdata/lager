# uart_ci_peer

Firmware for the UART peer used by section 15 of
`test/integration/communication/uart.sh`.

Sections 1-14 of that suite exercise argument parsing and net bookkeeping.
None of them move a byte over the wire, so a `lager uart` that connected,
reported a session and transferred nothing would pass all of them. This
firmware exists so one section can assert on real data in both directions.

## Protocol

Line based, LF terminated, 115200 8N1.

| Sent          | Replied              |
| ------------- | -------------------- |
| `PING`        | `PONG`               |
| `ID?`         | `LAGER-UART-PEER v1` |
| `ECHO <text>` | `<text>`             |
| `COUNT?`      | integer, incremented on every `COUNT?` since boot |
| `RESET`       | `OK` (clears the counter) |
| anything else | `ERR unknown`        |

Two properties are deliberate and should survive any edit:

**Every command echoes before it replies.** `lager uart -i` suppresses one
inbound line after each line it sends (`websocket_client.py:145`) without
checking that the discarded line matches what was sent. The echo absorbs that
suppression so the reply survives.

**Every command replies, including unrecognised ones.** A command that
answered with nothing would leave the CLI's suppression armed, and it would
swallow the *next* command's reply — presenting as flaky hardware.

There is no periodic output. Unsolicited traffic would make the assertions
racy.

## Hardware

Developed against an ESP32-DevKitM-1 (ESP32-MINI-1 module, **ESP32-U4WDH**),
whose onboard CP2102N exposes UART0 — so the same USB cable carries power,
flashing and the test traffic. No jumper wiring is required.

The U4WDH is **single core**. A stock Arduino-core image aborts on it with
`Running on single core variant of a chip, but app is built with multi-core
support`, and neither arduino-esp32 2.x nor 3.x ships a single-core target.
Hence ESP-IDF with `CONFIG_FREERTOS_UNICORE=y`, set in `sdkconfig.defaults`.
If you retarget this at a dual-core ESP32, that flag is unnecessary but
harmless.

## Build

Needs ESP-IDF v5.3.x (any install; nothing here depends on where it lives).

```bash
. $IDF_PATH/export.sh
idf.py set-target esp32
idf.py build
```

Confirm the flag actually took before flashing — getting this wrong produces a
boot loop that looks exactly like a failed flash:

```bash
grep CONFIG_FREERTOS_UNICORE sdkconfig      # must print =y
```

Optionally merge to a single image so the flash is one command:

```bash
esptool.py --chip esp32 merge_bin -o uart_peer_merged.bin \
  --flash_mode dio --flash_size 4MB --flash_freq 40m \
  0x1000 build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin \
  0x10000 build/uart_peer.bin
```

## Flash onto a bench board

The box already carries esptool in its container, so this runs from anywhere
with `lager` access — nobody needs to be at the bench. Substitute the box and
the device's tty.

```bash
scp uart_peer_merged.bin lagerdata@<BOX_IP>:/tmp/
lager ssh --box <BOX> -- docker cp /tmp/uart_peer_merged.bin lager:/tmp/
lager uart --sessions --box <BOX>        # nothing may hold the port
lager ssh --box <BOX> -- docker exec lager esptool.py --chip esp32 \
  --port /dev/ttyUSB1 --baud 460800 --before default_reset --after hard_reset \
  write_flash -z 0x0 /tmp/uart_peer_merged.bin
```

The DevKitM-1's auto-reset circuit (Q1/Q2, driven by DTR/RTS) lets esptool
enter the bootloader on its own, so the BOOT button is not needed.

## Enable the checks

Section 15 skips unless a peer answers `ID?`. Point it at the net:

```bash
UART_PEER_NET=ESP_UART bash test/integration/communication/uart.sh <BOX>
```

`UART_PEER_NET` defaults to `ESP_UART`. `UART_PEER_BAUD`, `UART_PEER_SETTLE`
and `UART_PEER_DEADLINE` are also overridable.
