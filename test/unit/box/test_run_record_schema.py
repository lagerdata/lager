# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Keep the run record's schema, example and specification in agreement.

docs/reference/run-record.v1.schema.json is normative for structure and
run-record.md for meaning. Consumers in other languages build from the
schema; people read the document. If a field appears in one and not the
other, one of them is wrong, and this is where that is caught.
"""

import json
import os
import re

import jsonschema

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
REF = os.path.join(REPO_ROOT, 'docs', 'reference')


def _load(name):
    with open(os.path.join(REF, name)) as f:
        return json.load(f)


def _property_names(schema, defs, seen=None):
    """Every property name anywhere in the schema, following $refs."""
    seen = seen if seen is not None else set()
    if isinstance(schema, dict):
        ref = schema.get('$ref')
        if ref:
            name = ref.rsplit('/', 1)[-1]
            if name not in seen:
                seen.add(name)
                yield from _property_names(defs[name], defs, seen)
        for key, value in schema.get('properties', {}).items():
            yield key
            yield from _property_names(value, defs, seen)
        for key in ('items', 'additionalProperties'):
            if isinstance(schema.get(key), dict):
                yield from _property_names(schema[key], defs, seen)
        for key in ('oneOf', 'anyOf', 'allOf'):
            for sub in schema.get(key, []):
                yield from _property_names(sub, defs, seen)


def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(_load('run-record.v1.schema.json'))


def test_example_validates():
    schema = _load('run-record.v1.schema.json')
    jsonschema.Draft202012Validator(schema).validate(_load('run-record.v1.example.json'))


def test_every_schema_field_is_described_in_the_specification():
    schema = _load('run-record.v1.schema.json')
    with open(os.path.join(REF, 'run-record.md')) as f:
        spec = f.read()
    described = set(re.findall(r'`([A-Za-z][A-Za-z0-9]*)`', spec))
    fields = set(_property_names(schema, schema.get('$defs', {})))
    missing = sorted(fields - described)
    assert not missing, f'in the schema but not described in run-record.md: {missing}'


def test_every_exit_reason_is_described():
    schema = _load('run-record.v1.schema.json')
    with open(os.path.join(REF, 'run-record.md')) as f:
        spec = f.read()
    reasons = schema['properties']['exit']['properties']['reason']['enum']
    missing = [r for r in reasons if f'| `{r}` |' not in spec]
    assert not missing, f'exit reasons with no row in run-record.md: {missing}'
