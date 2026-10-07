# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The box's half of the run record (box/lager/python/run_record.py).

The record is evidence, so these tests are about the properties that make it
evidence: that the box hashes what it actually received and emitted, that
the reason a run ended is stated correctly, that nothing escapes the counter,
and that a box whose records are collected refuses a run it cannot record
rather than running it unrecorded.

Records are written to a tmp directory and validated against the published
schema, so a field the code adds or drops without the schema following fails
here rather than in a consumer.
"""

import io
import json
import os
import zipfile

import jsonschema
import pytest

from lager.python import run_record as rr
from lager.util import jcs

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
SCHEMA_PATH = os.path.join(REPO_ROOT, 'docs', 'reference', 'run-record.v1.schema.json')


@pytest.fixture(scope='module')
def validator():
    with open(SCHEMA_PATH) as f:
        return jsonschema.Draft202012Validator(json.load(f))


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / 'run_records'
    monkeypatch.setenv(rr.ROOT_ENV, str(path))
    return str(path)


@pytest.fixture
def etc(tmp_path):
    etc = tmp_path / 'etc_lager'
    etc.mkdir()
    (etc / 'version').write_text('0.53.1|0.53.0\n')
    (etc / 'ref').write_text('v0.53.1@af96ba48\n')
    (etc / 'saved_nets.json').write_text(json.dumps([
        {'name': 'vbat', 'role': 'power-supply', 'instrument': 'Keithley_2281S',
         'address': 'USB0::0x05E6::0x2281::4512345::INSTR', 'channel': 1, 'limit': 4.2},
        {'name': 'ibat', 'role': 'battery', 'instrument': 'Keithley_2281S',
         'address': 'USB0::0x05E6::0x2281::4512345::INSTR'},
        {'name': 'uart0', 'role': 'uart', 'instrument': 'Prolific_PL2303',
         'address': 'serial://067b:23a3/serial/00000006'},
    ]))
    host = tmp_path / 'host_etc'
    host.mkdir()
    (host / 'hostname').write_text('bench-07\n')
    (host / 'os-release').write_text('PRETTY_NAME="Ubuntu 24.04.3 LTS"\nID=ubuntu\nVERSION_ID="24.04"\n')
    return str(etc), str(host)


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    """Point the download allowlist at a tmp directory."""
    from lager.binaries import store
    out = tmp_path / 'lager-output'
    out.mkdir()
    monkeypatch.setattr(store, 'ALLOWED_DOWNLOAD_ROOTS', (str(out),))
    return out


def collected(root):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, 'policy.json'), 'w') as f:
        json.dump({'mode': 'collected'}, f)


def begin(etc, **kwargs):
    etc_dir, host_dir = etc
    kwargs.setdefault('script_file', io.BytesIO(b'print("hi")\n'))
    kwargs.setdefault('script_name', 'rtd.py')
    return rr.RunRecorder.begin(kwargs.pop('run_id', '3f6c2a9e-8d41-4b7a-9e2f-1c5d7b0a6e13'),
                                etc=etc_dir, host_etc=host_dir, sys_devices='/nonexistent',
                                **kwargs)


def final(root, run_id):
    with open(os.path.join(root, 'final', f'{run_id}.json')) as f:
        return json.load(f)


# ------------------------------------------------------------------- JCS ---

class TestJcs:
    """RFC 8785, against the cases where Python's json module differs."""

    def test_keys_sort_by_utf16_code_units_not_code_points(self):
        # U+1F600 is one code point above U+FFFF but sorts BELOW it in UTF-16
        # (surrogate 0xD83D < 0xFFFF). Code-point order gets this backwards.
        value = {'￿': 1, '\U0001f600': 2, 'a': 3}
        assert jcs.canonicalize(value) == '{"a":3,"\U0001f600":2,"￿":1}'.encode()

    @pytest.mark.parametrize('number,expected', [
        (1e-7, '1e-7'), (1e16, '10000000000000000'), (1e21, '1e+21'),
        (123.456, '123.456'), (0.000001, '0.000001'), (-0.0, '0'), (4.2, '4.2'),
        (5e-324, '5e-324'), (1.7976931348623157e308, '1.7976931348623157e+308'),
    ])
    def test_numbers_are_written_as_ecmascript_writes_them(self, number, expected):
        assert jcs.canonicalize(number) == expected.encode()

    def test_strings_escape_only_what_rfc8785_requires(self):
        assert jcs.canonicalize('a"b\\c\n\u0001é') == b'"a\\"b\\\\c\\n\\u0001\xc3\xa9"'

    def test_no_whitespace(self):
        assert jcs.canonicalize({'b': [1, True, None], 'a': {}}) == b'{"a":{},"b":[1,true,null]}'

    def test_nan_is_refused(self):
        with pytest.raises(ValueError):
            jcs.canonicalize(float('nan'))


# ------------------------------------------------------------- box state ---

class TestInstruments:
    @pytest.mark.parametrize('address,expected', [
        ('USB0::0x05E6::0x2281::4512345::INSTR', ('4512345', 'usb-device')),
        ('USB0::0x1A86::0x7523::port-1-1.2::INSTR', (None, None)),
        ('USB0::0x1A86::0x7523::::INSTR', (None, None)),
        ('ppk2:C4A1B2', ('C4A1B2', 'usb-device')),
        ('serial://067b:23a3/serial/00000006', ('00000006', 'usb-adapter')),
        ('serial://067b:23a3/port/1-1.4', (None, None)),
        ('TCPIP0::10.0.0.5::inst0::INSTR', (None, None)),
        ('', (None, None)),
    ])
    def test_serial_comes_from_the_address(self, address, expected):
        assert rr.serial_from_address(address) == expected

    def test_one_entry_per_unit_listing_every_net_on_it(self):
        nets = [
            {'name': 'vbat', 'instrument': 'Keithley_2281S', 'address': 'USB0::0x05E6::0x2281::45::INSTR'},
            {'name': 'ibat', 'instrument': 'Keithley_2281S', 'address': 'USB0::0x05E6::0x2281::45::INSTR'},
            {'name': 'v2', 'instrument': 'Keithley_2281S', 'address': 'USB0::0x05E6::0x2281::99::INSTR'},
        ]
        units = rr.instruments_from_nets(nets)
        assert [(u['serialNumber'], u['nets']) for u in units] == [
            ('45', ['ibat', 'vbat']), ('99', ['v2'])]

    def test_address_falls_back_to_the_mapping_override(self):
        nets = [{'name': 'g', 'instrument': 'LabJack_T7',
                 'mappings': [{'device_override': 'USB0::0x0CD5::0x0007::470012::INSTR'}]}]
        assert rr.instruments_from_nets(nets)[0]['serialNumber'] == '470012'


class TestBoxIdentity:
    def test_box_id_is_minted_once_and_kept(self, root):
        first = rr.store_box_id(root)
        assert first and first != 'unknown'
        assert rr.store_box_id(root) == first

    def test_hardware_id_skips_virtual_interfaces(self, tmp_path):
        sys_devices = tmp_path / 'devices'
        for path, mac in [
            ('virtual/net/docker0/address', '02:42:ac:11:00:01'),
            ('pci0000:00/0000:00:1f.6/net/eno1/address', '3c:7c:3f:1a:2b:9e'),
            ('pci0000:00/0000:00:14.3/net/wlo1/address', '3c:7c:3f:1a:2b:9f'),
        ]:
            target = sys_devices / path
            target.parent.mkdir(parents=True)
            target.write_text(mac + '\n')
        assert rr.hardware_id(str(sys_devices)) == 'mac:3c:7c:3f:1a:2b:9e'

    def test_hardware_id_is_null_with_no_physical_interface(self, tmp_path):
        assert rr.hardware_id(str(tmp_path)) is None


def test_clock_state_has_the_recorded_shape():
    clock = rr.clock_state()
    assert set(clock) == {'synced', 'maxErrorMicroseconds'}
    assert clock['synced'] in (True, False, None)


# -------------------------------------------------------- client section ---

class TestClientField:
    def test_values_never_survive_into_environment_names(self):
        parsed = rr.parse_client_field(json.dumps({'passenv': ['GOOD', 'BAD=secret', '1X']}))
        assert parsed['passenv'] == ['GOOD']

    def test_malformed_field_is_ignored(self):
        assert rr.parse_client_field(b'{not json') == {}

    def test_shapes_are_checked(self):
        parsed = rr.parse_client_field(json.dumps({
            'cliVersion': 7,
            'git': {'commit': 'not-a-sha', 'dirty': False},
            'files': [{'path': 'a.py', 'size': -1, 'sha256': 'x'},
                      {'path': 'b.py', 'repoPath': 'tests/b.py', 'size': 1, 'sha256': 'a' * 64}],
            'labels': {'ok': 'v', 'bad key': 'v', 'n': 3},
            'repoRelativeRoot': 'tests',
        }))
        asserted = parsed['asserted']
        assert asserted['cliVersion'] is None
        assert asserted['git'] is None
        assert [f['path'] for f in asserted['files']] == ['b.py']
        assert asserted['labels'] == {'ok': 'v'}
        assert asserted['repoRelativeRoot'] == 'tests'

    def test_labels_are_capped(self):
        labels = {f'k{i}': 'v' for i in range(100)}
        parsed = rr.parse_client_field(json.dumps({'labels': labels}))
        assert len(parsed['asserted']['labels']) == rr.MAX_LABELS


def test_script_assertions_last_write_wins_and_bad_lines_drop(tmp_path):
    path = tmp_path / 'a.script'
    path.write_text('\n'.join([
        json.dumps({'key': 'dut.firmware', 'value': '1.0'}),
        'garbage',
        json.dumps({'key': 'bad key', 'value': 1}),
        json.dumps({'key': 'dut.firmware', 'value': '2.14.0'}),
        json.dumps({'key': 'dut.temp', 'value': 23.5}),
        json.dumps({'key': 'obj', 'value': {'nested': 1}}),
    ]))
    assert rr.read_script_assertions(str(path)) == {'dut.firmware': '2.14.0', 'dut.temp': 23.5}


def test_record_value_writes_where_the_box_reads(tmp_path, monkeypatch):
    from lager.run_assertions import record_value
    path = tmp_path / 'x.script'
    monkeypatch.setenv('LAGER_RUN_ASSERTIONS', str(path))
    assert record_value('dut.firmware', '2.14.0') is True
    assert record_value('dut.ok', True) is True
    assert rr.read_script_assertions(str(path)) == {'dut.firmware': '2.14.0', 'dut.ok': True}
    with pytest.raises(ValueError):
        record_value('bad key', 1)
    with pytest.raises(ValueError):
        record_value('k', {'nested': 1})


def test_record_value_is_a_no_op_outside_a_recorded_run(monkeypatch):
    from lager.run_assertions import record_value
    monkeypatch.delenv('LAGER_RUN_ASSERTIONS', raising=False)
    assert record_value('dut.firmware', '1') is False


# -------------------------------------------------------------- recorder ---

class TestRecorder:
    def test_a_finished_run_validates_against_the_schema(self, root, etc, downloads, validator):
        (downloads / 'out.csv').write_bytes(b'a,b\n1,2\n')
        recorder = begin(etc, args=[b'--cycles', b'3'], timeout_seconds=300,
                         client_field=json.dumps({'passenv': ['DUT_SERIAL'],
                                                  'downloads': [str(downloads / 'out.csv')],
                                                  'labels': {'execution': 'EX-1'}}))
        recorder.log_frame(1, b'hello\n')
        recorder.log_frame(0, b'')          # keepalive: not part of the log
        recorder.log_frame(2, b'')          # empty frame: not part of the log
        recorder.log_frame(2, b'warn\n')
        recorder.finalize(0)

        record = final(root, recorder.run_id)
        validator.validate(record)
        assert record['exit'] == {'code': 0, 'reason': 'exited', 'timeoutSeconds': 300,
                                  'cancelRequestedAt': None}
        assert record['code']['files'][0]['path'] == 'rtd.py'
        assert record['code']['files'][0]['sha256'] == rr._sha256(b'print("hi")\n')
        assert record['code']['args'] == ['--cycles', '3']
        assert record['environmentPassed'] == ['DUT_SERIAL']
        expected_log = b'1 6 hello\n' + b'2 5 warn\n'
        assert record['log']['sha256'] == rr._sha256(expected_log)
        assert record['log']['size'] == len(expected_log)
        assert record['outputs'] == [{'name': str(downloads / 'out.csv'), 'size': 8,
                                      'sha256': rr._sha256(b'a,b\n1,2\n'), 'retained': False}]
        assert record['boxState']['instruments'][0]['serialNumber'] == '4512345'
        assert record['boxState']['os']['versionId'] == '24.04'
        assert record['boxState']['lagerVersion'] == '0.53.1'
        assert record['box']['boxName'] == 'bench-07'
        assert record['clientAsserted']['labels'] == {'execution': 'EX-1'}

    def test_module_records_every_file_in_the_zip(self, root, etc):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('main.py', b'import helper\n')
            zf.writestr('calibration.csv', b'1,2\n')
            zf.writestr('lib/helper.py', b'X = 1\n')
        data = buf.getvalue()
        recorder = begin(etc, script_file=None, module_zip=io.BytesIO(data))
        recorder.finalize(0)
        code = final(root, recorder.run_id)['code']
        assert code['kind'] == 'module'
        assert code['archiveSha256'] == rr._sha256(data)
        assert [f['path'] for f in code['files']] == ['calibration.csv', 'lib/helper.py', 'main.py']

    def test_the_upload_is_left_rewound_for_the_executor(self, root, etc):
        upload = io.BytesIO(b'print(1)\n')
        begin(etc, script_file=upload)
        assert upload.read() == b'print(1)\n'

    def test_sequence_counts_every_accepted_run(self, root, etc):
        first = begin(etc, run_id='a1')
        second = begin(etc, run_id='a2')
        second.finalize(None, ended='start-failed')
        first.finalize(0)
        assert final(root, 'a1')['boxSequence'] + 1 == final(root, 'a2')['boxSequence']

    def test_a_reused_run_id_gets_a_fresh_one(self, root, etc):
        first = begin(etc, run_id='dup')
        second = begin(etc, run_id='dup')
        assert first.run_id == 'dup' and second.run_id != 'dup'

    def test_an_unsafe_run_id_is_replaced(self, root, etc):
        assert begin(etc, run_id='../../etc/passwd').run_id != '../../etc/passwd'

    def test_timeout(self, root, etc, monkeypatch):
        recorder = begin(etc, timeout_seconds=3)
        recorder.accepted_monotonic -= 10
        recorder.finalize(-9)
        assert final(root, recorder.run_id)['exit'] == {
            'code': 137, 'reason': 'timeout', 'timeoutSeconds': 3, 'cancelRequestedAt': None}

    def test_exit_124_before_the_deadline_is_the_script_not_the_timeout(self, root, etc):
        recorder = begin(etc, timeout_seconds=300)
        recorder.finalize(124)
        assert final(root, recorder.run_id)['exit']['reason'] == 'exited'

    def test_cancelled_wins_and_records_when(self, root, etc):
        recorder = begin(etc, timeout_seconds=3)
        rr.mark_cancelled(recorder.run_id)
        recorder.accepted_monotonic -= 10
        recorder.finalize(137)
        exit_ = final(root, recorder.run_id)['exit']
        assert exit_['reason'] == 'cancelled'
        assert exit_['cancelRequestedAt'].endswith('Z')

    def test_kill_all_marks_every_open_run(self, root, etc):
        a, b = begin(etc, run_id='k1'), begin(etc, run_id='k2')
        rr.mark_cancelled(None)
        a.finalize(-15)
        b.finalize(-15)
        assert {final(root, r)['exit']['reason'] for r in ('k1', 'k2')} == {'cancelled'}

    def test_disconnected(self, root, etc):
        recorder = begin(etc)
        recorder.finalize(-2, ended='disconnected')
        assert final(root, recorder.run_id)['exit'] == {
            'code': 130, 'reason': 'disconnected', 'timeoutSeconds': 0, 'cancelRequestedAt': None}

    def test_finalize_runs_once(self, root, etc):
        recorder = begin(etc)
        recorder.finalize(0)
        recorder.finalize(1, ended='disconnected')
        assert final(root, recorder.run_id)['exit']['code'] == 0

    def test_script_assertions_land_in_the_record(self, root, etc):
        recorder = begin(etc)
        with open(recorder.env[rr.SCRIPT_ASSERT_ENV], 'a') as f:
            f.write(json.dumps({'key': 'dut.firmware', 'value': '2.14.0'}) + '\n')
        recorder.finalize(0)
        assert final(root, recorder.run_id)['scriptAsserted'] == {'dut.firmware': '2.14.0'}


class TestModes:
    def test_local_mode_keeps_no_bytes_and_no_outbox(self, root, etc, downloads):
        (downloads / 'out.bin').write_bytes(b'x' * 10)
        recorder = begin(etc, client_field=json.dumps({'downloads': [str(downloads / 'out.bin')]}))
        recorder.log_frame(1, b'hi')
        recorder.finalize(0)
        record = final(root, recorder.run_id)
        assert record['retention'] == {'mode': 'local', 'maxBlobBytes': 0}
        assert record['log']['retained'] is False
        assert record['outputs'][0]['retained'] is False
        assert os.listdir(os.path.join(root, 'outbox')) == []
        assert os.listdir(os.path.join(root, 'blobs')) == []

    def test_collected_mode_retains_bytes_by_hash(self, root, etc, downloads):
        collected(root)
        (downloads / 'out.bin').write_bytes(b'payload')
        recorder = begin(etc, client_field=json.dumps({'downloads': [str(downloads / 'out.bin')]}))
        recorder.log_frame(1, b'hi')
        recorder.finalize(0)
        record = final(root, recorder.run_id)
        assert record['retention']['mode'] == 'collected'
        assert os.path.exists(os.path.join(root, 'outbox', f'{recorder.run_id}.json'))
        for sha, expected in [(record['log']['sha256'], b'1 2 hi'),
                              (record['outputs'][0]['sha256'], b'payload')]:
            with open(os.path.join(root, 'blobs', sha), 'rb') as f:
                assert f.read() == expected

    def test_collected_mode_marks_oversized_bytes_unretained(self, root, etc, monkeypatch):
        collected(root)
        monkeypatch.setenv(rr.BLOB_MAX_ENV, '4')
        monkeypatch.setattr(rr, 'FREE_SPACE_SLACK', 0)
        recorder = begin(etc)
        recorder.log_frame(1, b'more than four bytes')
        recorder.finalize(0)
        record = final(root, recorder.run_id)
        assert record['log']['retained'] is False
        assert record['retention']['maxBlobBytes'] == 4
        assert record['log']['size'] == len(b'1 20 more than four bytes')

    def test_collected_mode_refuses_a_run_it_cannot_record(self, root, etc, monkeypatch):
        collected(root)

        class Full:
            f_bavail = 1
            f_frsize = 4096
        monkeypatch.setattr(rr.os, 'statvfs', lambda path: Full())
        with pytest.raises(rr.RunRecordUnavailable):
            begin(etc)

    def test_local_mode_runs_unrecorded_rather_than_refusing(self, root, etc, monkeypatch):
        monkeypatch.setattr(rr, 'next_sequence', lambda root: (_ for _ in ()).throw(OSError('disk')))
        assert begin(etc) is None


class TestLostAndFetch:
    def test_a_restart_finalizes_open_runs_as_lost(self, root, etc, validator):
        collected(root)
        recorder = begin(etc, timeout_seconds=60)
        recorder.log_frame(1, b'partial')
        recorder._log_file.flush()
        rr.mark_cancelled(recorder.run_id)
        # The process dies here: no finalize.
        assert rr.sweep_lost() == 1
        record = final(root, recorder.run_id)
        validator.validate(record)
        assert record['exit']['reason'] == 'lost'
        assert record['exit']['code'] is None
        assert record['finishedAt'] is None
        assert record['outputs'] == []
        assert record['log'] == {'sha256': rr._sha256(b'1 7 partial'), 'size': 11, 'retained': False}
        assert record['exit']['cancelRequestedAt'] is not None
        assert os.listdir(os.path.join(root, 'open')) == []
        assert os.path.exists(os.path.join(root, 'outbox', f'{recorder.run_id}.json'))

    def test_fetch(self, root, etc):
        recorder = begin(etc)
        assert rr.load_final(recorder.run_id, wait_s=0) == (202, None)
        recorder.finalize(0)
        status, record = rr.load_final(recorder.run_id, wait_s=0)
        assert status == 200 and record['runId'] == recorder.run_id
        assert rr.load_final('never-ran', wait_s=0) == (404, None)
        assert rr.load_final('../escape', wait_s=0) == (404, None)

    def test_final_is_pruned_but_outbox_is_not(self, root, etc, monkeypatch):
        collected(root)
        monkeypatch.setattr(rr, 'FINAL_KEEP', 2)
        for i in range(4):
            begin(etc, run_id=f'p{i}').finalize(0)
        assert len(os.listdir(os.path.join(root, 'final'))) == 2
        assert len(os.listdir(os.path.join(root, 'outbox'))) == 4

