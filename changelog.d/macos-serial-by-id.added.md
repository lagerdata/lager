- **`/dev/serial/by-id` nets work on a Mac Box.** A net saved on a Linux Box
  with a `/dev/serial/by-id/...` port opens unchanged on a Mac. The Mac also has
  `/dev/serial/by-path/platform-macos-...` names of its own. A by-path name
  saved on a Linux Box names that machine's USB socket, so it is refused on a
  Mac with an error that says to use the by-id name instead.
