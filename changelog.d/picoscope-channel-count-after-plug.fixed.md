- **A PicoScope scanned right after it is plugged in no longer lists two
  channels on a four-channel unit.** The scope daemon takes a few seconds to
  open a unit, and a scan in that gap (`lager instruments`, or the `lager nets`
  TUI, which scans once at launch) fell back to the two-channel default. The
  scan now waits up to 8 s for the daemon to finish opening it. A scope the
  daemon cannot open is waited on once a minute, and a box with no PicoScope
  or no daemon is not waited on at all.
