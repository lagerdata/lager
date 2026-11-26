- **`lager nets tui` can make FTDI GPIO, I2C and SPI nets on channels B-D.** On
  an FT2232H or FT4232H, adding one of these nets now asks for the FTDI channel
  (A-D for GPIO on an FT4232H, A-B for I2C, SPI and the FT2232H) and saves it as
  `params.interface`, the record `lager nets add --interface` makes. Before, every
  net the TUI made was on channel A, and a saved net hid its pin on every other
  channel. The Net Manager now lists these nets with their channel (`C:1`), and a
  net saved without an interface counts as channel A. FT232H nets are unchanged.
