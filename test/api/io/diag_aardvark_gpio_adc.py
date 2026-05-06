#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Aardvark GPIO + ADC diagnostic -- runs on the box via `lager python`.

Toggles each Aardvark GPIO pin (bits 0-5) and reads all 14 ADC channels
after each toggle to identify wiring and detect whether the Aardvark is
actually driving voltage.

Run with:
    lager python test/api/io/diag_aardvark_gpio_adc.py --box <YOUR-BOX>

No arguments needed -- auto-discovers everything.
"""
import sys
import time


PIN_NAMES = {0: "SCL", 1: "SDA", 2: "MISO", 3: "SCK", 4: "MOSI", 5: "SS"}
NUM_ADC = 14  # adc1 .. adc14


def read_all_adc(verbose=False):
    """Read all 14 ADC channels, return dict of name -> voltage."""
    from lager import Net, NetType
    results = {}
    for i in range(1, NUM_ADC + 1):
        name = f"adc{i}"
        try:
            adc = Net.get(name, type=NetType.ADC)
            results[name] = adc.input()
        except Exception as e:
            results[name] = None
            if verbose:
                print(f"    {name} error: {type(e).__name__}: {e}")
    return results


def test_adc_standalone():
    """Test ADC access before touching the Aardvark."""
    print("\n--- Step 0: ADC standalone test (before opening Aardvark) ---")
    from lager import Net, NetType

    # Try just adc1 with full error traceback
    for name in ["adc1", "adc2"]:
        try:
            adc = Net.get(name, type=NetType.ADC)
            v = adc.input()
            print(f"  {name}: {v:.4f} V  (type={type(adc).__name__})")
        except Exception as e:
            import traceback
            print(f"  {name} FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()

    # Also try direct LabJack access
    print("\n  Direct LabJack LJM test:")
    try:
        from labjack import ljm
        handle = ljm.openS("T7", "ANY", "ANY")
        info = ljm.getHandleInfo(handle)
        print(f"  Opened LabJack: type={info[0]}, serial={info[2]}")
        ain0 = ljm.eReadName(handle, "AIN0")
        ain1 = ljm.eReadName(handle, "AIN1")
        print(f"  AIN0 = {ain0:.4f} V")
        print(f"  AIN1 = {ain1:.4f} V")
        ljm.close(handle)
    except ImportError:
        print("  labjack ljm not importable")
    except Exception as e:
        print(f"  LabJack error: {type(e).__name__}: {e}")


def main():
    print("=" * 70)
    print("Aardvark GPIO -> ADC Diagnostic")
    print("=" * 70)

    # --- Step 0: Test ADC independently first ---
    test_adc_standalone()

    # --- Step 1: Check Aardvark detection via aardvark_py ---
    print("\n--- Step 1: Aardvark device detection ---")
    try:
        import aardvark_py as aa
        devices = aa.aa_find_devices(16)
        # aa_find_devices returns (num_devices, ports_array)
        num = devices[0] if isinstance(devices, tuple) else devices
        ports = devices[1] if isinstance(devices, tuple) else []
        print(f"  Devices found: {num}")
        if hasattr(ports, '__len__'):
            for i, p in enumerate(ports[:num]):
                in_use = " (IN USE)" if p & 0x8000 else ""
                print(f"    Port {p & 0x7FFF}{in_use}")
        if num == 0:
            print("  ERROR: No Aardvark devices detected!")
            return 1
    except ImportError:
        print("  ERROR: aardvark_py not installed on this box")
        return 1
    except Exception as e:
        print(f"  ERROR: {e}")
        return 1

    # --- Step 2: Open Aardvark and configure GPIO ---
    print("\n--- Step 2: Open and configure Aardvark ---")
    try:
        handle = aa.aa_open(0)
        print(f"  aa_open(0) = {handle}")
        if handle < 0:
            print(f"  ERROR: Failed to open (error {handle})")
            return 1

        # GPIO-only mode (0x00)
        cfg = aa.aa_configure(handle, 0x00)
        print(f"  aa_configure(GPIO_ONLY) = {cfg}")

        # Read current direction and pin state
        cur_dir = aa.aa_gpio_direction(handle, 0x00)  # read current, set to 0
        cur_get = aa.aa_gpio_get(handle)
        print(f"  Current direction mask: 0x{cur_dir:02x} (before test)")
        print(f"  Current pin state:      0x{cur_get:02x}")
    except Exception as e:
        print(f"  ERROR opening Aardvark: {e}")
        return 1

    # --- Step 3: Baseline ADC readings (all GPIO pins as input) ---
    print("\n--- Step 3: Baseline ADC (all Aardvark pins as input) ---")
    aa.aa_gpio_direction(handle, 0x00)  # all inputs
    time.sleep(0.2)
    baseline = read_all_adc(verbose=True)
    for name, v in baseline.items():
        if v is not None:
            print(f"  {name}: {v:8.4f} V")
        else:
            print(f"  {name}: (not available)")

    # --- Step 4: Toggle each GPIO pin and read ADC ---
    print("\n--- Step 4: Toggle each Aardvark GPIO pin ---")
    print("  Looking for ADC channels that change when a pin goes HIGH...")
    print()

    for bit in range(6):
        pin_mask = 1 << bit
        pin_name = PIN_NAMES[bit]

        # Set this pin as output HIGH, all others as input
        aa.aa_gpio_direction(handle, pin_mask)
        aa.aa_gpio_set(handle, pin_mask)
        time.sleep(0.3)

        # Read all ADC
        high_readings = read_all_adc()

        # Set pin LOW
        aa.aa_gpio_set(handle, 0x00)
        time.sleep(0.3)

        # Read all ADC again
        low_readings = read_all_adc()

        # Reset pin to input
        aa.aa_gpio_direction(handle, 0x00)

        # Find channels that changed significantly
        print(f"  Bit {bit} ({pin_name}, header pin {[1,3,5,7,8,9][bit]}):")
        found_change = False
        for name in sorted(high_readings.keys(), key=lambda x: int(x.replace("adc", ""))):
            vh = high_readings.get(name)
            vl = low_readings.get(name)
            if vh is None or vl is None:
                continue
            delta = vh - vl
            if abs(delta) > 0.5:
                found_change = True
                print(f"    ** {name}: HIGH={vh:.3f}V  LOW={vl:.3f}V  delta={delta:+.3f}V **")
            # Also show if HIGH reading is in 2.5-4.0V range (expected for 3.3V GPIO)
            elif 2.0 < vh < 4.5:
                found_change = True
                print(f"    ?  {name}: HIGH={vh:.3f}V  LOW={vl:.3f}V  delta={delta:+.3f}V (possible)")
        if not found_change:
            print(f"    (no ADC channel responded)")
        print()

    # --- Step 5: Drive all pins HIGH at once ---
    print("--- Step 5: All pins HIGH simultaneously ---")
    aa.aa_gpio_direction(handle, 0x3F)  # all 6 as output
    aa.aa_gpio_set(handle, 0x3F)        # all HIGH
    time.sleep(0.3)
    all_high = read_all_adc()

    aa.aa_gpio_set(handle, 0x00)  # all LOW
    time.sleep(0.3)
    all_low = read_all_adc()

    aa.aa_gpio_direction(handle, 0x00)  # back to input

    print("  ADC readings (all HIGH vs all LOW):")
    for name in sorted(all_high.keys(), key=lambda x: int(x.replace("adc", ""))):
        vh = all_high.get(name)
        vl = all_low.get(name)
        if vh is None or vl is None:
            continue
        delta = vh - vl
        marker = " **" if abs(delta) > 0.5 else ""
        print(f"  {name}: HIGH={vh:8.4f}V  LOW={vl:8.4f}V  delta={delta:+.4f}V{marker}")

    # --- Step 6: Verify aa_gpio_get reads back pin state ---
    print("\n--- Step 6: Aardvark self-test (set + get) ---")
    for bit in range(6):
        pin_mask = 1 << bit
        pin_name = PIN_NAMES[bit]
        aa.aa_gpio_direction(handle, pin_mask)
        aa.aa_gpio_set(handle, pin_mask)
        time.sleep(0.05)
        readback = aa.aa_gpio_get(handle)
        # NOTE: aa_gpio_get on output pins may read 0 (known hardware behavior)
        aa.aa_gpio_direction(handle, 0x00)
        print(f"  Bit {bit} ({pin_name}): set=0x{pin_mask:02x}, "
              f"get=0x{readback:02x}, bit_set={bool(readback & pin_mask)}")

    # Cleanup
    aa.aa_gpio_direction(handle, 0x00)
    aa.aa_gpio_set(handle, 0x00)
    aa.aa_close(handle)
    print("\n--- Done (Aardvark closed) ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
