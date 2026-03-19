FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/lager

RUN apt-get update && apt-get install -y ca-certificates libusb-1.0-0-dev libudev-dev \
	libhidapi-dev git gcc python-dev-is-python3 python3-pip python3-venv xz-utils build-essential bluetooth ssh openssh-client \
	cups-client lpr zlib1g-dev wget libjpeg-dev libpng-dev libfreetype6-dev \
	fswebcam automake g++ libtool libleptonica-dev make pkg-config libpango1.0-dev gdb-multiarch tesseract-ocr libtesseract-dev \
	flex bison ccache ninja-build \
	libturbojpeg0-dev v4l-utils \
	wireless-tools \
	tini \
	gnupg \
	&& wget -qO /usr/share/keyrings/phidgets.gpg https://www.phidgets.com/gpgkey/pubring.gpg \
	&& echo deb [signed-by=/usr/share/keyrings/phidgets.gpg] http://www.phidgets.com/debian bookworm main > /etc/apt/sources.list.d/phidgets.list \
	&& apt-get update \
	&& apt-get install -y libphidget22 \
	&& rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3 10 \
    && python -m pip install --upgrade \
      pip \
      virtualenv \
    && :

# PicoScope SDK is mounted from host at runtime via start_box.sh
# The SDK library is at /opt/picoscope/lib/libps2000.so on the host
# This avoids the systemd reload error that occurs when installing in Docker





# MCC uldaq C library -- required by uldaq Python bindings for USB-202 DAQ devices
# See: https://github.com/mccdaq/uldaq
RUN apt-get update && apt-get install -y autoconf automake libtool libusb-1.0-0-dev && rm -rf /var/lib/apt/lists/* \
	&& git clone --depth 1 https://github.com/mccdaq/uldaq.git /tmp/uldaq \
	&& cd /tmp/uldaq \
	&& autoreconf -i \
	&& ./configure \
	&& make \
	&& make install \
	&& ldconfig \
	&& rm -rf /tmp/uldaq

# LabJack LJM SDK -- proprietary, downloaded at build time
# See: https://support.labjack.com/docs/ljm-software-installer-downloads-t4-t7-t8-digit
# Docker install pattern from: https://github.com/labjack/ljm_docker
RUN apt-get update && apt-get install -y unzip && rm -rf /var/lib/apt/lists/* \
	&& wget -q https://files.labjack.com/installers/LJM/Linux/x64/release/LabJack-LJM_2024-06-10.zip \
	&& unzip LabJack-LJM_2024-06-10.zip \
	&& ./labjack_ljm_installer.run -- --without-kipling --no-restart-device-rules \
	&& ldconfig \
	&& rm -f LabJack-LJM_2024-06-10.zip labjack_ljm_installer.run \
	&& test -f /usr/local/lib/libLabJackM.so

RUN /usr/local/bin/python -m pip install --upgrade pip \
&& pip3 install --upgrade setuptools \
&& pip3 install wheel \
&& pip3 install --no-cache-dir \
	'bleak==0.22.2' \
	'requests==2.31.0' \
	'pyserial==3.5' \
	'beautifulsoup4==4.12.0' \
	'esptool==4.6.2' \
	'pytest==6.2.5' \
	'redis==4.0.2' \
	'PyYAML==6.0.1' \
	'ptyprocess==0.7.0' \
	'pexpect==4.8.0' \
	'pexpect-serial==0.1.0' \
	'pyusb==1.2.1' \
	'hidapi==0.14.0' \
	'simplejson==3.18.0' \
	'labjack-ljm==1.23.0' \
	'pygdbmi==0.11.0.0' \
	'cryptography==38.0.4' \
	'psycopg2-binary==2.9.9' \
	'pyvisa-py==0.5.2' \
	'PyVISA==1.11.3' \
	'Phidget22==1.19.20240311' \
	'opencv-python-headless==4.10.0.84' \
	'git+https://github.com/Yepkit/pykush@d83ee856b4d0ea961b63b531fe4f4c533f6962f6' \
	'yoctopuce' \
	'joulescope' \
	'ppk2-api' \
	'aardvark_py' \
	'pyftdi' \
	'uldaq' \
	'Flask==3.0.0' \
	'flask-socketio==5.3.5' \
	'python-socketio==5.10.0' \
	'websockets==12.0' \
	'rich' \
	'cbor2' \
	'websocket-client>=1.6.0' \
	'git+https://github.com/Vaskivskyi/asusrouter.git@8de97bfa8ffe3efa2f6d1ec30bb95187d13ab37a'

RUN git config --global http.version HTTP/1.1

# Install nrfutil for Nordic DFU/OTA updates
# nrfutil v7+ is a standalone binary, not a pip package
# NRFUTIL_HOME must be set so subcommands are installed to a shared location
# accessible by both root (build) and www-data (runtime)
ENV NRFUTIL_HOME=/opt/nrfutil
RUN mkdir -p /opt/tools /opt/nrfutil \
    && wget -q https://developer.nordicsemi.com/.pc-tools/nrfutil/x64-linux/nrfutil -O /opt/tools/nrfutil \
    && chmod +x /opt/tools/nrfutil \
    && /opt/tools/nrfutil install nrf5sdk-tools \
    && /opt/tools/nrfutil install device \
    && chown -R www-data:www-data /opt/nrfutil \
    && chmod -R 755 /opt/nrfutil

# Install user-specified packages from user_requirements.txt
COPY docker/user_requirements.txt /tmp/user_requirements.txt
RUN pip3 install -r /tmp/user_requirements.txt

# Copy Python lager package modules (grouped structure)
# Copy Python files at root level
COPY __init__.py actuate.py box_http_server.py control_plane_client.py dashboard_db.py cache.py constants.py core.py \
     exceptions.py hardware_service.py log.py rotation.py util.py \
     /app/lager/lager/

# Copy core subpackages
COPY binaries /app/lager/lager/binaries
COPY debug /app/lager/lager/debug
COPY dispatchers /app/lager/lager/dispatchers
COPY exec /app/lager/lager/exec
COPY http_handlers /app/lager/lager/http_handlers
COPY instrument_wrappers /app/lager/lager/instrument_wrappers
COPY nets /app/lager/lager/nets
COPY python /app/lager/lager/python

# Copy grouped hardware modules
# Power group: supply, battery, solar, eload
COPY power /app/lager/lager/power
# I/O group: adc, dac, gpio
COPY io /app/lager/lager/io
# Measurement group: scope, thermocouple, watt
COPY measurement /app/lager/lager/measurement
# Protocols group: uart, ble, wifi
COPY protocols /app/lager/lager/protocols
# BluFi: ESP32 WiFi provisioning via BLE
COPY blufi /app/lager/lager/blufi
# Automation group: arm, usb_hub, webcam
COPY automation /app/lager/lager/automation

COPY run.sh /app
# Acroname BrainStem SDK (USB hub control)
# See: https://pypi.org/project/brainstem/
RUN pip3 install brainstem --upgrade

RUN usermod -aG dialout www-data
RUN usermod -aG plugdev www-data
RUN usermod -aG bluetooth www-data
RUN usermod -aG lpadmin www-data
RUN usermod -aG video www-data

RUN mkdir -p /var/www
RUN chown -R www-data:www-data /var/www

# Create /etc/lager directory for saved nets
RUN mkdir -p /etc/lager
RUN chown -R www-data:www-data /etc/lager

# Copy startup script that starts debug service
COPY docker/start-services.sh /usr/local/bin/start-services.sh
RUN chmod +x /usr/local/bin/start-services.sh

# Oscilloscope streaming daemon (PicoScope support)
# The daemon binary is mounted from the host at runtime via start_box.sh
# Location: /home/lagerdata/third_party/oscilloscope-daemon -> /usr/local/bin/oscilloscope-daemon
# Build instructions: cd box/oscilloscope-daemon && ./build_daemon.sh

# Copy oscilloscope web visualization files
COPY docker/web_oscilloscope.html /app/lager/web_oscilloscope.html

# Use tini as init system to reap zombie processes
# This prevents zombie processes from accumulating when debug tools (JLink, GDB) are started/stopped
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/start-services.sh"]

USER www-data

LABEL version=v0.1.92
