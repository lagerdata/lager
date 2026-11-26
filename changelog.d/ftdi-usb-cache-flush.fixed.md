- An FTDI part (FT232H, FT2232H, FT4232H) that is plugged back in can be opened
  again at once where libusb follows hotplug itself, as on a macOS Box. The first
  `lager gpi`, `gpo`, `i2c` or `spi` call used to fail with "No such device",
  or keep failing if a call ran while the part was unplugged, because pyftdi
  still listed the old device. A Linux Box is unchanged.
