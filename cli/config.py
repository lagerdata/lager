# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.config

    Config file management routines

    NOTE: .lager config files must be in JSON format only.
    INI format is not supported.
"""
import os
import json
import configparser
import click

DEFAULT_CONFIG_FILE_NAME = '.lager'

# Module-level caches for config file parsing
# Keyed by file path, stores parsed ConfigParser objects
_config_cache = {}
# Keyed by file path, stores file modification times
_config_cache_mtime = {}
LAGER_CONFIG_FILE_NAME = os.getenv('LAGER_CONFIG_FILE_NAME', DEFAULT_CONFIG_FILE_NAME)

DEVENV_SECTION_NAME = 'DEVENV'
DEBUG_SECTION_NAME = 'DEBUG'


def _json_to_configparser(json_data):
    """Convert JSON data to ConfigParser object."""
    config = configparser.ConfigParser()

    # Add LAGER section
    if 'LAGER' not in config:
        config.add_section('LAGER')

    # Convert DEFAULTS section if it exists in JSON (or legacy LAGER)
    defaults_data = json_data.get('DEFAULTS') or json_data.get('LAGER')
    if defaults_data and isinstance(defaults_data, dict):
        for key, value in defaults_data.items():
            config.set('LAGER', key, str(value))

    # Convert DEVENV section if it exists (check both DEVENV and legacy 'devenv')
    devenv_data = json_data.get('DEVENV') or json_data.get('devenv')
    if devenv_data:
        if DEVENV_SECTION_NAME not in config:
            config.add_section(DEVENV_SECTION_NAME)
        for key, value in devenv_data.items():
            config.set(DEVENV_SECTION_NAME, key, str(value))

    # Convert DEBUG section if it exists (check both DEBUG and legacy 'debug')
    debug_data = json_data.get('DEBUG') or json_data.get('debug')
    if debug_data:
        if DEBUG_SECTION_NAME not in config:
            config.add_section(DEBUG_SECTION_NAME)
        for key, value in debug_data.items():
            config.set(DEBUG_SECTION_NAME, key, str(value))

    return config


def _configparser_to_json(config, existing_json=None):
    """Convert ConfigParser object to JSON data, preserving existing JSON fields."""
    if existing_json is None:
        json_data = {}
    else:
        json_data = existing_json.copy()

    # Convert LAGER section to DEFAULTS in JSON
    if config.has_section('LAGER'):
        defaults_data = {}
        for key, value in config.items('LAGER'):
            defaults_data[key] = value
        if defaults_data:  # Only add if non-empty
            json_data['DEFAULTS'] = defaults_data
        else:
            # Remove DEFAULTS if LAGER section is empty
            json_data.pop('DEFAULTS', None)
        # Remove legacy LAGER key if it exists
        json_data.pop('LAGER', None)

    # Convert DEVENV section to DEVENV JSON object
    if config.has_section(DEVENV_SECTION_NAME):
        devenv_data = {}
        for key, value in config.items(DEVENV_SECTION_NAME):
            devenv_data[key] = value
        if devenv_data:
            json_data['DEVENV'] = devenv_data
            # Remove legacy 'devenv' key if it exists
            json_data.pop('devenv', None)

    # Convert DEBUG section to DEBUG JSON object
    if config.has_section(DEBUG_SECTION_NAME):
        debug_data = {}
        for key, value in config.items(DEBUG_SECTION_NAME):
            debug_data[key] = value
        if debug_data:
            json_data['DEBUG'] = debug_data
            # Remove legacy 'debug' key if it exists
            json_data.pop('debug', None)

    return json_data


def get_global_config_file_path():
    if 'LAGER_CONFIG_FILE_DIR' in os.environ:
        return make_config_path(os.getenv('LAGER_CONFIG_FILE_DIR'))
    return make_config_path(os.path.expanduser('~'))

def make_config_path(directory, config_file_name=None):
    """
        Make a full path to a lager config file
    """
    if config_file_name is None:
        config_file_name = LAGER_CONFIG_FILE_NAME

    return os.path.join(directory, config_file_name)

def find_devenv_config_path():
    """
        Find a local .lager config, if it exists
    """
    configs = _find_config_files()
    if not configs:
        return None
    return configs[0]

def _find_config_files():
    """
        Search up from current directory for all .lager files
    """
    cwd = os.getcwd()
    cfgs = []
    global_config_file_path = get_global_config_file_path()
    while True:
        config_path = make_config_path(cwd)
        if os.path.exists(config_path) and config_path != global_config_file_path:
            cfgs.append(config_path)
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent

    return cfgs


def read_config_file(path=None):
    """
        Read our config file into `config` object.
        Only JSON format is supported.
        Uses caching with file modification time checks to avoid re-parsing.
    """
    if path is None:
        path = get_global_config_file_path()

    # Check if file exists - if not, return empty config (no caching needed)
    if not os.path.exists(path):
        config = configparser.ConfigParser()
        config.add_section('LAGER')
        return config

    # Check cache and file modification time
    try:
        current_mtime = os.path.getmtime(path)
    except OSError:
        # If we can't get mtime, skip caching and parse fresh
        current_mtime = None

    # Return cached config if file hasn't been modified
    if current_mtime is not None:
        if path in _config_cache and _config_cache_mtime.get(path) == current_mtime:
            return _config_cache[path]

    # Parse the config file (JSON only)
    config = configparser.ConfigParser()

    try:
        with open(path, 'r') as f:
            json_data = json.load(f)
        config = _json_to_configparser(json_data)
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as e:
        from .errors import LagerError
        raise LagerError(
            f'The config file {path} is not valid JSON.',
            cause=f'{e} (.lager files must be JSON, not INI/key=value).',
            fixes=[
                f'Open {path} and fix the syntax at the position above.',
                'Compare against a known-good .lager, or delete it to start fresh.',
            ],
            raw=e,
        )

    if 'LAGER' not in config:
        config.add_section('LAGER')

    # Cache the result with its modification time
    if current_mtime is not None:
        _config_cache[path] = config
        _config_cache_mtime[path] = current_mtime

    return config

def write_config_file(config, path=None):
    """
        Write out `config` into our config file.
        Always writes in JSON format.
        Invalidates the config cache for this path.
    """
    if path is None:
        path = get_global_config_file_path()

    # Invalidate cache for this path since we're writing new content
    _config_cache.pop(path, None)
    _config_cache_mtime.pop(path, None)

    # Read existing JSON data if file exists
    existing_json = {}
    if os.path.exists(path):
        with open(path, 'r') as f:
            try:
                existing_json = json.load(f)
            except json.JSONDecodeError:
                existing_json = {}

    # Convert config to JSON while preserving existing fields
    json_data = _configparser_to_json(config, existing_json)

    # Write as JSON
    with open(path, 'w') as f:
        json.dump(json_data, f, indent=2)

def read_lager_json(path=None):
    """
        Read .lager JSON file directly.
        Returns empty dict if file doesn't exist or isn't valid JSON.
    """
    if path is None:
        path = find_devenv_config_path()

    if path is None or not os.path.exists(path):
        return {}

    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

def write_lager_json(data, path=None):
    """
        Write .lager JSON file directly.
        Invalidates the config cache for this path.
    """
    if path is None:
        path = find_devenv_config_path()
        if path is None:
            raise ValueError("No .lager config path found")

    # Invalidate cache for this path since we're writing new content
    _config_cache.pop(path, None)
    _config_cache_mtime.pop(path, None)

    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def devenv_config_list(value):
    """
        Normalize a DEVENV config value into a list of strings.

        Accepts a JSON array (the canonical form), a single string, or None.
        A string is split on newlines so newline-separated values also work.
        Used for the `volumes` and `environment` DEVENV keys.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def get_devenv_json():
    """
        Return a path and JSON data for devenv.
        Exits with error if no config file is found or JSON is invalid.
    """
    from .errors import LagerError

    config_path = find_devenv_config_path()
    if config_path is None:
        raise LagerError(
            f'No {LAGER_CONFIG_FILE_NAME} config file found.',
            cause=f'Searched {os.getcwd()} and every parent directory.',
            fixes=[
                f'Run this command from inside your project (where {LAGER_CONFIG_FILE_NAME} lives), or',
                f'Create one: add a {LAGER_CONFIG_FILE_NAME} JSON file in your project root.',
            ],
        )

    try:
        with open(config_path, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise LagerError(
            f'The config file {config_path} is not valid JSON.',
            cause=f'{e} (.lager files must be JSON).',
            fixes=[f'Open {config_path} and fix the syntax at the position above.'],
            raw=e,
        )
    except IOError as e:
        raise LagerError(
            f'Could not read the config file {config_path}.',
            cause=str(e),
            fixes=['Check the file exists and you have permission to read it.'],
            raw=e,
        )

    return config_path, data

def get_includes_from_config(config_path=None):
    """
        Read the 'includes' section from a .lager config file (JSON format only).
        Returns a dictionary mapping destination paths to source paths,
        with source paths resolved relative to the config file location.

        Example .lager file:
        {
            "includes": {
                "dtest": "../dtest",
                "shared": "/abs/path/to/shared"
            }
        }

        Returns: {"dtest": "/abs/path/to/dtest", "shared": "/abs/path/to/shared"}
    """
    if config_path is None:
        config_path = find_devenv_config_path()

    if config_path is None or not os.path.exists(config_path):
        return {}

    includes = {}

    try:
        with open(config_path, 'r') as f:
            json_data = json.load(f)

        # Get includes section (support both 'includes' and 'INCLUDES')
        includes_data = json_data.get('includes') or json_data.get('INCLUDES')

        if includes_data and isinstance(includes_data, dict):
            # Resolve relative paths relative to the config file location
            config_dir = os.path.dirname(os.path.abspath(config_path))

            for dest_path, source_path in includes_data.items():
                # If source_path is relative, resolve it relative to config file
                if not os.path.isabs(source_path):
                    abs_source = os.path.abspath(os.path.join(config_dir, source_path))
                else:
                    abs_source = source_path

                includes[dest_path] = abs_source
    except (json.JSONDecodeError, IOError):
        # If JSON parsing fails, return empty dict
        return {}

    return includes


def get_debug_script_for_net(netname):
    """
    Get J-Link script path for a debug net from local .lager config.

    The script path is looked up in the DEBUG section of the local .lager
    config file (JSON format only). Relative paths are resolved relative
    to the config file location.

    Args:
        netname: Name of the debug net to look up script for

    Returns:
        Absolute path to the J-Link script file if found and exists,
        None otherwise.

    Example .lager config:
        {
            "DEBUG": {
                "SWD": "./scripts/my_device.JLinkScript",
                "my-debug-net": "/absolute/path/to/script.JLinkScript"
            }
        }
    """
    config_path = find_devenv_config_path()
    if not config_path:
        return None

    config = read_config_file(config_path)
    if not config.has_section(DEBUG_SECTION_NAME):
        return None

    try:
        script_path = config.get(DEBUG_SECTION_NAME, netname)
    except configparser.NoOptionError:
        return None

    # Resolve relative paths relative to the config file location
    if not os.path.isabs(script_path):
        config_dir = os.path.dirname(os.path.abspath(config_path))
        script_path = os.path.abspath(os.path.join(config_dir, script_path))

    # Verify the script file exists
    if not os.path.exists(script_path):
        return None

    return script_path
