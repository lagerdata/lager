- **On a Mac Box, a `lager python` script reaches every net on a shared
  instrument.** GPIO, ADC, DAC, SPI and I2C nets saved with the same address
  (several pins of one FTDI or LabJack) were all driven and read through
  whichever of those nets the host agent opened first. Each now reaches its
  own pin, as `lager gpo`, `lager adc` and the other commands already did.
