# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

import os
import setuptools

import sys
sys.path.insert(0, os.path.dirname(__file__))
from __init__ import __version__ as lager_version

def readme():
    path = os.path.dirname(__file__)
    with open(os.path.join(path, 'README.md')) as f:
        return f.read()

name = 'lager-cli'
description = 'Lager CLI - Box and Docker connectivity'
author = 'Lager Data LLC'
classifiers = [
    'Development Status :: 3 - Alpha',
    'License :: OSI Approved :: Apache Software License',
    'Programming Language :: Python :: 3',
    'Programming Language :: Python :: 3.10',
    'Programming Language :: Python :: 3.11',
    'Programming Language :: Python :: 3.12',
    'Programming Language :: Python :: 3.13',
    'Programming Language :: Python :: 3.14',
    'Topic :: Software Development',
]

if __name__ == "__main__":
    setuptools.setup(
        name=name,
        version=lager_version,
        description=description,
        long_description=readme(),
        long_description_content_type='text/markdown',
        classifiers=classifiers,
        url='https://github.com/lagerdata/lager',
        author=author,
        maintainer=author,
        license='Apache-2.0',
        python_requires=">=3.10",
        packages=['cli'] + ['cli.' + p for p in setuptools.find_packages(where='.')],
        package_dir={'cli': '.'},
        package_data={
            'cli.deployment.scripts': ['*.sh'],
            'cli.deployment.security': ['*.sh'],
        },
        include_package_data=True,
        install_requires='''
            certifi >= 2020.6.20
            click >= 8.1.2
            colorama >= 0.4.3
            PyYAML >= 6.0.1
            requests >= 2.31.0
            requests-toolbelt >= 1.0.0
            tenacity >= 6.2.0
            texttable >= 1.6.2
            trio >= 0.27.0
            trio-websocket
            urllib3 >= 1.26.20, < 3.0.0
            wsproto >= 0.14.1
            textual >= 3.2.0
            python-socketio >= 5.10.0
            websocket-client >= 1.0.0
        ''',
        extras_require={
            'mcp': ['mcp>=1.20.0'],
        },
        project_urls={
            'Bug Reports': 'https://github.com/lagerdata/lager/issues',
            'Documentation': 'https://docs.lagerdata.com',
            'Source': 'https://github.com/lagerdata/lager',
        },
        entry_points={
            'console_scripts': [
                'lager=cli.main:main',
                'lager-mcp=cli.mcp.server:main',
            ],
        }
    )