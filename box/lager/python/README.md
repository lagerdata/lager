# Lager Python Execution Service

## Overview

This module provides HTTP endpoints for executing Python scripts on the Lager box. It has been migrated from the controller container to run directly in the Python container.

**Runs In**: `box/python` container (port 5000)

## Architecture

### Architecture
```
CLI → DirectHTTPSession (port 5000) → python container (this service) → docker exec (self)
```

## Key Benefits

1. **Simpler Architecture**: Removes controller container dependency
2. **Fewer Hops**: Direct execution in the Python container
3. **Better Modularity**: All Python-related code in one container
4. **Cleaner Separation**: Controller can eventually be removed

## Module Structure

```
lager/python/
├── __init__.py          # Module exports
├── README.md            # This file
├── exceptions.py        # Python execution exceptions
├── executor.py          # PythonExecutor class (core logic)
└── service.py           # HTTP service (Flask-like endpoints)
```

## API Endpoints

All endpoints run on **port 5000** (same as before, but now in Python container).

### POST /python
Execute a Python script in the container.

**Request**: Multipart form data with:
- `script`: Python script file (optional if module provided)
- `module`: Zip file containing Python module (optional if script provided)
- `args`: Command-line arguments (list)
- `env`: Environment variables (list of "KEY=value" strings)
- `timeout`: Maximum execution time in seconds (default: 300)
- `detach`: Run in detached mode ("true"/"false")
- `lock_holder`: box-lock holder string, detached runs only. Its presence asks
  the box to hold that lock for the job's lifetime and release it when the job
  ends. Optional: absent from an older CLI, and deliberately absent when the run
  resumed a pre-existing lock rather than acquiring one.
- `stdout_is_stderr`: Redirect stderr to stdout ("true"/"false", default: true)
- `muxes`: Multiplexer configuration JSON (optional)
- `usb_mapping`: USB device mapping JSON (optional)
- `dut_commands`: DUT command configuration JSON (optional)

**Response**: Streaming output with format:
```
<fileno> <length> <data>
```
Where fileno is:
- 0: Keepalive (empty chunk every 20s)
- 1: stdout
- 2: stderr
- 3: output_channel (Lager internal)
- Final line: `- <len> <returncode>`

**Response when `detach` is set**: a JSON body, written before the job starts:
```json
{
  "status": "detached",
  "pid": null,
  "lager_process_id": "<uuid>",
  "lock_held_by_box": false
}
```
`pid` is always null -- the response precedes the spawn, so no process exists
yet; the real pid lands in the job's `meta.json`. `lager_process_id` is the id
the client sent, or one the box minted when it sent none. `lock_held_by_box`
reports whether the box accepted a `lock_holder` and is heartbeating that lock.

Anything that goes wrong after this response -- a `requirements.txt` that will
not install, a module zip that will not open -- is written into the job's own
`output.log` as a stderr chunk plus an exit marker, and its `meta.json` reaches
`failed`. `POST /python/attach` is how a client finds out.

**Example**:
```bash
curl -X POST http://box-ip:5000/python \
  -F "script=@my_script.py" \
  -F "args=arg1" \
  -F "args=arg2" \
  -F "env=MY_VAR=value"
```

### POST /python/kill
Kill a running Python process.

**Request**: JSON body with:
```json
{
  "signal": 15,  // Signal number (optional, default: SIGTERM)
  "lager_process_id": "uuid"  // Process UUID (optional)
}
```

**Response**:
```json
{
  "status": "killed"
}
```

### POST /pip
Run pip commands in the container.

**Request**: JSON body with:
```json
{
  "args": ["install", "pandas"]
}
```

**Response**: Streaming output (same format as /python)

### GET /health
Health check endpoint.

**Response**:
```json
{
  "status": "healthy",
  "service": "lager-python-execution",
  "version": "1.0.0"
}
```

## Usage from CLI

The CLI automatically uses this service via `DirectHTTPSession`:

```python
# cli/context.py
session = DirectHTTPSession(box_ip)
response = session.run_python(box, files)
```

The CLI sends multipart form data with the script and all parameters. The service executes it and streams back the output.

## Environment Variables Injected

The executor automatically injects these environment variables into every Python script:

### Standard Lager Variables
- `PIGPIO_ADDR`: IP of pigpio container
- `LAGER_HOST`: Docker host IP
- `LAGER_HOST_MODULE_FOLDER`: Path to script/module on host
- `LAGER_OUTPUT_CHANNEL`: Path to output channel file
- `LAGER_STDOUT_IS_STDERR`: Whether stderr is redirected
- `PYTHONBREAKPOINT`: Set to `remote_pdb.set_trace`
- `LOCAL_ADDRESS`: Python container IP (172.18.0.10)
- `REMOTE_PDB_HOST`: Host for remote debugger (0.0.0.0)
- `REMOTE_PDB_PORT`: Port for remote debugger (5555)

### Box Metadata
- `LAGER_BOX_ID`: Box ID from `/etc/lager/box_id`
- `LAGER_CLIENT_IP`: IP address of the CLI client

### Organization Secrets
All keys from `/etc/lager/org_secrets.json` are injected as:
- `LAGER_SECRET_<key>`: Secret value

### Optional Configuration
- `LAGER_MUXES`: Multiplexer configuration (if provided)
- `LAGER_USB_MAPPINGS`: USB device mapping (if provided)
- `LAGER_DUT_COMMANDS`: DUT debug commands (if provided)

## Running the Service

### As a Standalone Service
```python
from lager.python import run_python_service

if __name__ == '__main__':
    run_python_service()  # Runs on 0.0.0.0:5000
```

### Programmatic Usage
```python
from lager.python import PythonExecutor

executor = PythonExecutor()
output_generator = executor.execute(
    script_file=open('my_script.py', 'rb'),
    args=['arg1', 'arg2'],
    env_vars=['MY_VAR=value'],
    timeout=300,
)

for chunk in output_generator:
    print(chunk)
```

## Dependencies

### External Packages
- None (uses Python stdlib only)

### Internal Modules
- `lager.exec.docker`: Docker container management
- `lager.exec.process`: Process output streaming

## See Also

- `box/lager/exec/`: Docker and process management utilities
- `box/lager/debug/`: Debug service (port 8765)
- `cli/context.py`: CLI integration code
