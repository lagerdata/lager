- **Serial-port supplies and the DexArm work on a Mac Box.** Rigol DP700 and EA
  PSB nets (`lager supply`, and the EA as a solar simulator) and DexArm nets
  (`lager arm`) open their port on a Mac as they do on a Linux Box, including a
  saved `serial://`, `/dev/ttyUSB0` or `/dev/ttyACM0` address. The host agent
  opens the Mac port behind each Linux port name.
