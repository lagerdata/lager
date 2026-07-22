# Box udev Rules

This directory contains the **shipped** udev rules for USB instruments the box
supports out of the box (`99-instrument.rules`). They are deployed to every box
during setup/update and pair with the `SUPPORTED_USB` instrument tables.

> **Just need a device to be openable from inside the container?** (e.g.
> `dfu-util` failing with "No DFU capable USB device available".) You don't
> need to edit this file or cut a release — add a user rule directly:
>
> ```bash
> lager box-config udev add 1209:0001 --box <BOX>   # add --usbtmc for SCPI
> lager box-config apply --box <BOX>
> ```
>
> That installs `/etc/udev/rules.d/99-lager-user.rules` on the box host and
> reloads udev. The rules below remain the home for first-class supported
> instruments.

## Current Rules

### `99-labjack.rules`
- **Device:** LabJack T7
- **VID:PID:** 0x0cd5:0x0007
- **Purpose:** Allows all users to access the LabJack T7 USB device
- **Fixes:** `LJME_DEVICE_CURRENTLY_CLAIMED_BY_ANOTHER_PROCESS` errors

## Adding New Rules

To add a new udev rule for another instrument:

1. **Find the device VID and PID:**
   ```bash
   lsusb
   # Look for your device, e.g.:
   # Bus 001 Device 005: ID 0cd5:0007 LabJack Corporation
   #                        ^^^^:^^^^
   #                        VID  PID
   ```

2. **Create a new `.rules` file:**
   ```bash
   # Name format: 99-<device-name>.rules
   # Example: 99-keysight-scope.rules
   ```

3. **Add the udev rule:**
   ```bash
   # Template:
   SUBSYSTEM=="usb", ATTRS{idVendor}=="<VID>", ATTRS{idProduct}=="<PID>", MODE="0660", GROUP="lager"

   # Example for Keysight scope (hypothetical):
   SUBSYSTEM=="usb", ATTRS{idVendor}=="0957", ATTRS{idProduct}=="1234", MODE="0660", GROUP="lager"
   ```

4. **Deploy using the deployment script:**
   ```bash
   cd deployment_scripts
   ./deploy_box.sh <box-ip>
   ```

## Rule Format

### Basic Permission Rule
```bash
SUBSYSTEM=="usb", ATTRS{idVendor}=="<VID>", ATTRS{idProduct}=="<PID>", MODE="0660", GROUP="lager"
```

- `SUBSYSTEM=="usb"`: Match USB devices only
- `ATTRS{idVendor}=="<VID>"`: Match specific vendor ID (4-digit hex)
- `ATTRS{idProduct}=="<PID>"`: Match specific product ID (4-digit hex)
- `MODE="0660", GROUP="lager"`: Read/write for the lager group only. The
  container user gets access via `--group-add` in start_box.sh; the host
  needs the group (`sudo groupadd -f lager` — `lager update` ensures this).

### SCPI/USBTMC Instruments (prevents "Resource busy" errors)

For SCPI instruments that use USB TMC (Test & Measurement Class), you need an
additional rule to unbind the `usbtmc` kernel driver. This allows PyVISA to
access the device directly via libusb, preventing "Resource busy" (Errno 16) errors.

```bash
# Permission rule (required)
SUBSYSTEM=="usb", ATTRS{idVendor}=="<VID>", ATTRS{idProduct}=="<PID>", MODE="0660", GROUP="lager"
# Unbind usbtmc driver when it binds (required for PyVISA access)
ACTION=="bind", SUBSYSTEM=="usb", DRIVER=="usbtmc", ATTRS{idVendor}=="<VID>", ATTRS{idProduct}=="<PID>", RUN+="/bin/sh -c 'echo %k > /sys/bus/usb/drivers/usbtmc/unbind 2>/dev/null || true'"
```

This pattern is needed for:
- Power supplies (Rigol DP8xx, Keysight E36xxx, Keithley)
- Oscilloscopes (Rigol MSO5xxx)
- Electronic loads (Rigol DL3xxx)
- Any instrument accessed via VISA USB address (e.g., `USB0::0x1AB1::0x0E11::...::INSTR`)

## Applying Rules Manually

If you need to manually apply rules on the box:

```bash
# Copy rule file to box
scp 99-<device>.rules lagerdata@<box-ip>:/tmp/

# SSH to box and install
ssh lagerdata@<box-ip>
sudo cp /tmp/99-<device>.rules /etc/udev/rules.d/
sudo chmod 644 /etc/udev/rules.d/99-<device>.rules
sudo udevadm control --reload-rules
sudo udevadm trigger

# Or simply unplug/replug the USB device
```

## Troubleshooting

### Permission errors persist after applying rules
1. Verify rule is installed: `ls -l /etc/udev/rules.d/99-*.rules`
2. Check rule syntax: `udevadm test $(udevadm info -q path -n /dev/bus/usb/001/005)`
3. Unplug and replug the USB device
4. Check device permissions: `ls -l /dev/bus/usb/*/*`

### Finding device path
```bash
# Find USB device path
lsusb
udevadm info -a -p $(udevadm info -q path -n /dev/bus/usb/001/005)
```

## References

- [udev man page](https://man7.org/linux/man-pages/man7/udev.7.html)
- [Writing udev rules](http://reactivated.net/writing_udev_rules.html)
