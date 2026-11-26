- **`POST /nets/power-up` reports how each device holds its levels.** Every
  device entry, and so `lager nets apply-power-up --json`, now carries
  `backend` (`labjack`), `storage` (`flash`: the instrument applies the level
  itself as it boots) and `coverage` (what covers the pin before then). This
  prepares power-up levels for instruments that keep no boot state.
