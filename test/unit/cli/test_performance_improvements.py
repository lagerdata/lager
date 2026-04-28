#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""
Performance Improvement Verification Tests for CLI.

Tests config file caching, connection pooling, streaming optimization,
and SSH connection reduction.
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


class TestConfigCaching:
    """Config file caching with mtime checks."""

    def test_config_cache_exists(self):
        """Verify config cache variables exist."""
        from cli.config import _config_cache, _config_cache_mtime

        assert isinstance(_config_cache, dict), "_config_cache should be a dict"
        assert isinstance(_config_cache_mtime, dict), "_config_cache_mtime should be a dict"

    def test_config_parsing_cached(self):
        """Verify config is cached on second read."""
        from cli.config import read_config_file, _config_cache, _config_cache_mtime

        # Create a temp config file. read_config_file accepts JSON only
        # (raises SystemExit on invalid JSON); _json_to_configparser maps the
        # top-level "LAGER" key to the LAGER configparser section.
        with tempfile.NamedTemporaryFile(mode='w', suffix='.lager', delete=False) as f:
            f.write('{"LAGER": {"test_key": "test_value"}}')
            temp_path = f.name

        try:
            # Clear cache first
            _config_cache.clear()
            _config_cache_mtime.clear()

            # First read - should parse and cache
            config1 = read_config_file(temp_path)
            assert temp_path in _config_cache, "Config should be cached after first read"
            assert temp_path in _config_cache_mtime, "Config mtime should be tracked"

            # Second read - should return cached version
            config2 = read_config_file(temp_path)
            assert config1 is config2, "Same config object should be returned from cache"

            # Verify value is correct
            assert config1.get('LAGER', 'test_key') == 'test_value'

        finally:
            os.unlink(temp_path)

    def test_config_cache_invalidation_on_write(self):
        """Verify cache is invalidated when config is written."""
        from cli.config import read_config_file, write_config_file, _config_cache

        with tempfile.NamedTemporaryFile(mode='w', suffix='.lager', delete=False) as f:
            f.write('{"LAGER": {"original_key": "original_value"}}')
            temp_path = f.name

        try:
            # Read to populate cache
            config1 = read_config_file(temp_path)
            assert temp_path in _config_cache

            # Write new config - should invalidate cache
            config1.set('LAGER', 'new_key', 'new_value')
            write_config_file(config1, temp_path)

            # Cache should be invalidated
            assert temp_path not in _config_cache, "Cache should be invalidated after write"

        finally:
            os.unlink(temp_path)


class TestConnectionPooling:
    """Connection pooling in DirectHTTPSession."""

    def test_session_pool_exists(self):
        """Verify DirectHTTPSession has class-level session pool."""
        from cli.context import DirectHTTPSession

        assert hasattr(DirectHTTPSession, '_session_pool'), "DirectHTTPSession should have _session_pool"
        assert isinstance(DirectHTTPSession._session_pool, dict), "_session_pool should be a dict"

    def test_session_reuse(self):
        """Verify sessions are reused for same box IP."""
        from cli.context import DirectHTTPSession

        # Clear the pool first
        DirectHTTPSession._session_pool.clear()

        # Create two sessions for same IP
        session1 = DirectHTTPSession('192.168.1.100')
        session2 = DirectHTTPSession('192.168.1.100')

        # They should share the same underlying requests.Session
        assert session1.session is session2.session, "Sessions for same IP should share connection pool"

        # Different IP should have different session
        session3 = DirectHTTPSession('192.168.1.101')
        assert session3.session is not session1.session, "Sessions for different IPs should be separate"

    def test_http_adapter_settings(self):
        """Verify HTTP adapter has proper pooling settings."""
        from cli.context import DirectHTTPSession
        from requests.adapters import HTTPAdapter

        DirectHTTPSession._session_pool.clear()
        session = DirectHTTPSession('192.168.1.100')

        # Get the adapter for http://
        adapter = session.session.get_adapter('http://test')

        # Verify it's an HTTPAdapter with pooling
        assert isinstance(adapter, HTTPAdapter), "Should use HTTPAdapter"

        # Verify pooling configuration via HTTPAdapter internal attributes
        assert hasattr(adapter, '_pool_connections'), "HTTPAdapter should have _pool_connections"
        assert adapter._pool_connections == 10, "pool_connections should be 10"
        assert adapter._pool_maxsize == 10, "pool_maxsize should be 10"


class TestStreamingOptimization:
    """Streaming optimization verification."""

    def test_flush_buffer_yields_chunks(self):
        """Verify flush_buffer yields data in chunks, not bytes."""
        from cli.context import DirectIPSession

        # Create a mock process to test streaming
        # The flush_buffer function is defined inside _create_streaming_response
        # We verify the implementation by checking the code structure
        import inspect
        source = inspect.getsource(DirectIPSession._create_streaming_response)

        # Check that the fixed pattern is present (yield header + buffer)
        assert 'yield header + buffer' in source, "flush_buffer should yield header + buffer as one chunk"

        # Check that byte-by-byte pattern is NOT present
        assert 'for byte in header:' not in source, "Should not iterate byte-by-byte over header"
        assert 'yield bytes([byte])' not in source, "Should not yield single bytes"

    def test_exit_code_yields_chunks(self):
        """Verify exit code path yields data in chunks, not bytes."""
        from cli.context import DirectIPSession

        import inspect
        source = inspect.getsource(DirectIPSession._create_streaming_response)

        # Check that exit code is yielded as one chunk
        assert 'yield header + exit_code_bytes' in source, "Exit code should be yielded as one chunk"


class TestSSHConnectionReduction:
    """SSH connection reduction verification."""

    def test_combined_ssh_command(self):
        """Verify module upload uses combined SSH command."""
        from cli.context import DirectIPSession

        import inspect
        source = inspect.getsource(DirectIPSession.run_python)

        # Check for the combined command pattern
        assert 'combined_cmd' in source, "Should use combined_cmd for SSH operations"

        # Verify extract + docker cp + run are in one command
        assert 'zipfile.ZipFile' in source, "Should include zip extraction"
        assert 'docker cp' in source, "Should include docker cp"

        # Check the consolidation comment
        assert 'Consolidate extract, docker cp, and run' in source or \
               '2nd connection' in source, "Should document the SSH consolidation"


def run_config_cache_benchmark():
    """Benchmark config file caching performance."""
    from cli.config import read_config_file, _config_cache, _config_cache_mtime

    # Create a test config file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.lager', delete=False) as f:
        f.write('[LAGER]\n')
        for i in range(100):
            f.write(f'key_{i} = value_{i}\n')
        temp_path = f.name

    try:
        # Clear cache
        _config_cache.clear()
        _config_cache_mtime.clear()

        # Benchmark uncached read
        iterations = 100

        start = time.time()
        for _ in range(iterations):
            _config_cache.clear()
            _config_cache_mtime.clear()
            read_config_file(temp_path)
        uncached_time = time.time() - start

        # Benchmark cached read
        _config_cache.clear()
        _config_cache_mtime.clear()
        read_config_file(temp_path)  # Prime the cache

        start = time.time()
        for _ in range(iterations):
            read_config_file(temp_path)
        cached_time = time.time() - start

        print(f"\nConfig Caching Benchmark ({iterations} iterations):")
        print(f"  Uncached: {uncached_time:.4f}s ({uncached_time/iterations*1000:.2f}ms per read)")
        print(f"  Cached:   {cached_time:.4f}s ({cached_time/iterations*1000:.2f}ms per read)")
        print(f"  Speedup:  {uncached_time/cached_time:.1f}x faster with caching")

    finally:
        os.unlink(temp_path)


if __name__ == '__main__':
    print("=" * 60)
    print("Performance Improvement Verification")
    print("=" * 60)

    # Run unit tests
    import pytest
    pytest.main([__file__, '-v', '--tb=short'])

    # Run benchmark
    print("\n" + "=" * 60)
    print("Running Benchmarks")
    print("=" * 60)
    run_config_cache_benchmark()
