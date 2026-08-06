# Lager Execution Utilities (`lager.exec`)

## Overview

This module provides low-level utilities for Docker container management and process execution. It was extracted from the controller container to support the Python execution service.

**Migration Date**: 2025-01-13
**Migrated From**: `gateway/controller/controller/application/views/run.py` (legacy, removed)
**Used By**: `lager.python` (Python execution service)

## Module Structure

```
lager/exec/
├── __init__.py      # Module exports
├── README.md        # This file
├── docker.py        # Docker container operations
└── process.py       # Process output streaming
```

## Components

### `docker.py` - Docker Container Management

Utilities for interacting with Docker containers.

#### Key Functions

**`execute_in_container(container_name, command, ...)`**
Execute a command inside a Docker container using `docker exec`.

```python
from lager.exec.docker import execute_in_container

proc = execute_in_container(
    container_name='python',
    command=['/usr/local/bin/python3', 'script.py'],
    workdir='/tmp',
    env_vars={'MY_VAR': 'value'},
    timeout=300,
)
```

**`get_container_ip(container_name, network_name)`**
Get the IP address of a container on a specific network.

```python
from lager.exec.docker import get_container_ip

ip = get_container_ip('python', 'lagernet')
# Returns: '172.18.0.10'
```

**`get_container_pid(proc, container_name)`**
Get the PID of a running container.

```python
pid = get_container_pid(proc, 'python')
```

**`is_container_running(container_name)`**
Check if a container is running.

```python
from lager.exec.docker import is_container_running

if is_container_running('python'):
    print("Container is running")
```

**`kill_container_process(container_name, signal)`**
Send a signal to a container and remove it.

```python
import signal
from lager.exec.docker import kill_container_process

kill_container_process('python', signal.SIGTERM)
```

#### Constants

- `CONTAINER_NAME = 'python'`: Default Python container name
- `PIGPIO_CONTAINER_NAME = 'pigpio'`: Pigpio container name
- `LAGER_NETWORK_NAME = 'lagernet'`: Docker network name

### `process.py` - Process Output Streaming

Utilities for streaming process output over HTTP with multiplexing.

#### Key Functions

**`stream_process_output(proc, output_channel, cleanup_fns)`**
Stream output from a process with multiplexed stdout/stderr/output_channel.

```python
from lager.exec.process import stream_process_output, make_output_channel

cleanup_fns = set()
output_channel = make_output_channel(cleanup_fns)

for chunk in stream_process_output(proc, output_channel, cleanup_fns):
    if not chunk:
        # IDLE_TICK — carries no data. Check on the client here; write nothing.
        continue
    # chunk format: b'<fileno> <length> <data>'
    yield chunk
```

**Output Format**:
Each chunk is prefixed with: `<fileno> <length> <data>`
- fileno 0: Keepalive (sent every 20s)
- fileno 1: stdout
- fileno 2: stderr
- fileno 3: output_channel
- Final line: `- <len> <returncode>`

**Idle ticks**: while the script is quiet the generator also yields `IDLE_TICK`
(zero bytes, ~10x/second). It is not part of the wire format and **must be
skipped, not written**. It exists so the consumer regains control often enough
to notice a client that has gone away — otherwise a disconnect during a silent
stretch goes unseen until the script prints again, and the script keeps driving
the bench in the meantime. `send_streaming_response` uses it to run
`peer_is_connected()`.

**`make_output_channel(cleanup_fns)`**
Create a temporary file for the output channel.

```python
output_channel = make_output_channel(cleanup_fns)
# Returns a NamedTemporaryFile object
```

**`terminate_process(proc)`**
Terminate a process, escalating only as far as needed: SIGINT, then SIGTERM,
then SIGKILL. SIGINT comes first so the script's `except KeyboardInterrupt` /
`finally` / `atexit` teardown actually runs — this is the path that releases
bench hardware when a client disconnects.

```python
returncode = terminate_process(proc)
```

**`add_cleanup_fn(cleanup_fns, fn, *args, **kwargs)`**
Register a cleanup function to be called later.

```python
add_cleanup_fn(cleanup_fns, os.unlink, '/tmp/file.txt')
```

**`do_cleanup(cleanup_fns)`**
Execute all registered cleanup functions.

```python
do_cleanup(cleanup_fns)
```

#### Constants

- `KEEPALIVE_TIME = 20`: Seconds between keepalive messages
- `IDLE_TICK = b''`: Zero-length sentinel yielded while the script is quiet
- `CLEANUP_GRACE_S = 5.0`: Seconds a script may go **without making progress**
  after SIGINT before escalating. An idle budget, not a total: a teardown that
  keeps working keeps its deadline pushed out. Progress is CPU time and context
  switches across the script's process tree, so cleanup blocked on an instrument
  counts as working.
- `CLEANUP_MAX_S = 60.0`: Absolute ceiling on cleanup, however busy it looks
- `TERMINATE_GRACE_S = 2.0`: Seconds after SIGTERM before SIGKILL

## Usage Examples

### Execute Python Script in Container

```python
from lager.exec.docker import execute_in_container
from lager.exec.process import make_output_channel, stream_process_output

# Setup
cleanup_fns = set()
output_channel = make_output_channel(cleanup_fns)

# Execute
proc = execute_in_container(
    container_name='python',
    command=['/usr/local/bin/python3', '/tmp/script.py'],
    workdir='/tmp',
    env_vars={'FOO': 'bar'},
    timeout=300,
)

# Stream output
for chunk in stream_process_output(proc, output_channel, cleanup_fns):
    print(chunk.decode(), end='')
```

### Check Container Status

```python
from lager.exec.docker import is_container_running, get_container_ip

if is_container_running('python'):
    ip = get_container_ip('python', 'lagernet')
    print(f"Python container running at {ip}")
else:
    print("Python container is not running")
```

### Kill a Container

```python
import signal
from lager.exec.docker import kill_container_process

# Gracefully terminate
kill_container_process('python', signal.SIGTERM)

# Force kill
kill_container_process('python', signal.SIGKILL)
```

## Design Decisions

### Why Separate from `lager.python`?

1. **Reusability**: These utilities could be used by other services
2. **Testing**: Easier to test in isolation
3. **Clarity**: Clear separation between "what" (Python execution) and "how" (Docker/process management)

### Why Not Use Docker SDK?

The current implementation uses `subprocess` to call `docker` CLI directly because:
1. **Simplicity**: No external dependencies
2. **Control**: Direct access to all docker CLI features
3. **Compatibility**: Works everywhere docker CLI works
4. **Performance**: Minimal overhead for our use case

### Output Stream Multiplexing

The output streaming format (`<fileno> <length> <data>`) allows multiplexing multiple streams (stdout, stderr, output_channel) over a single HTTP response. This is necessary because:

1. **HTTP Limitation**: HTTP responses are single-stream
2. **Keepalives**: Need to send periodic messages to prevent timeouts
3. **Metadata**: Need to send return code at the end
4. **Debugging**: Need separate streams for stdout vs stderr

The CLI (`cli/util.py:stream_python_output`) parses this format back into separate streams.

## Dependencies

### External
- None (uses Python stdlib only)

### System Requirements
- Docker CLI (`/usr/bin/docker`)
- `/usr/bin/timeout` command
- Unix-like OS (uses `select.select()`)

## Performance Considerations

### Streaming
- **Buffer Size**: 1024 bytes per read (configurable)
- **Keepalive**: Every 20 seconds to prevent HTTP timeouts
- **Select Timeout**: 0.1s for responsive streaming

### Process Management
- **Graceful Shutdown**: 2 second timeout before SIGKILL
- **PID Detection**: Up to 50 retries with 50ms delay (2.5s total)

## Error Handling

All functions use exceptions for error handling:
- `subprocess.CalledProcessError`: Docker command failed
- `subprocess.TimeoutExpired`: Process timeout exceeded
- `FileNotFoundError`: Container or file not found
- `Exception`: Generic errors logged and re-raised

## Testing

### Unit Tests
```python
# Test container check
assert is_container_running('python') == True
assert is_container_running('nonexistent') == False

# Test IP retrieval
ip = get_container_ip('python', 'lagernet')
assert ip.startswith('172.')
```

### Integration Tests
```python
# Test script execution
proc = execute_in_container(
    'python',
    ['python3', '-c', 'print("hello")'],
)
output = proc.stdout.read()
assert b'hello' in output
```

## See Also

- `lager.python`: Python execution service (uses this module)
- `lager.debug`: Debug service
- `gateway/controller/`: Original implementation (legacy, removed)
