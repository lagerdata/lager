- **`lager nets tui` can make FTDI GPIO, I2C and SPI nets on channels B-D.** On
  an FT2232H or FT4232H, the Add list now shows each pin once per FTDI channel.
  GPIO rows run from `A:0` to `D:7` on an FT4232H. I2C and SPI rows are on A
  and B only. The TUI saves the channel as `params.interface`, the record
  `lager nets add --interface` makes. Before, every net the TUI made was on
  channel A, and a saved net hid its pin on every other channel. The Net
  Manager now lists these nets with their channel (`C:1`). A net saved without
  an interface counts as channel A. FT232H nets are unchanged.
