/*
 * Copyright 2024-2026 Lager Data
 * SPDX-License-Identifier: Apache-2.0
 *
 * Lager webcam viewer. Talks only to the stream's own server, on the port the
 * page came from:
 *
 *   GET  /stream               the MJPEG picture
 *   GET  /api/info             net, camera, size, fps, frame age, origin
 *   GET  /api/streams          every stream on the box, for the Net picker
 *   GET  /api/focus            {mode, value}
 *   GET  /api/brightness       {value}
 *   GET  /api/modes            the sizes and frame rates the camera offers
 *   GET  /api/controls         exposure and gain, when the camera has them
 *   POST /api/zoom/set|in|out|reset, /api/focus/auto|manual, /api/brightness/set,
 *        /api/pan/set, /api/orientation/set, /api/mode/set, /api/controls/set
 *   GET  /snapshot             one JPEG, for the Snapshot button
 *
 * On a box behind an access gateway the page runs on a gateway session
 * cookie. When that session ends the gateway answers 401, and the page says
 * how to get a new link rather than freezing on the last frame.
 */
(function () {
  'use strict';

  var SIDEBAR_STORAGE_KEY = 'lager-webcam-sidebar';
  var SIDEBAR_HIDDEN_CLASS = 'is-sidebar-hidden';
  // A frame older than this means the camera stopped delivering.
  var STALLED_AFTER_SECONDS = 3;
  var INFO_POLL_MS = 1000;
  var SETTINGS_POLL_MS = 2000;
  var STREAMS_POLL_MS = 5000;
  var CONTROLS_POLL_MS = 5000;
  // One arrow-key press pans this fraction of what is on screen.
  var PAN_KEY_STEP = 0.1;
  // Light debounce for sliders: snappy, without a request per pixel.
  var SLIDER_DEBOUNCE_MS = 75;

  function $(id) { return document.getElementById(id); }

  var state = {
    net: null,
    signedOut: false,
    zoom: 1,
    view: { pan_x: 0.5, pan_y: 0.5, rotate: 0, flip_h: false, flip_v: false },
    modes: [],
    mode: null,
    // Set while the user drags a slider, so a poll does not yank it back.
    dragging: {},
  };

  // ---------- requests ----------

  function signedOut() {
    if (state.signedOut) return;
    state.signedOut = true;
    setLive('error', 'SIGNED OUT');
    showEmpty('This viewer’s sign-in has ended.',
              ['Run ', code('lager webcam url'), ' for a new link.']);
    $('feed').removeAttribute('src');
  }

  // Resolves to parsed JSON, or null on any failure. A 401 or 403 means the
  // gateway session ended, which no retry will fix.
  function request(path, body, isPost) {
    var options = { cache: 'no-store', credentials: 'same-origin' };
    if (isPost) options.method = 'POST';
    if (body !== undefined) {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify(body);
    }
    return fetch(path, options).then(function (response) {
      if (response.status === 401 || response.status === 403) {
        signedOut();
        return null;
      }
      if (!response.ok) return null;
      return response.json();
    }).catch(function () { return null; });
  }

  // A POST with no body when none is given: the server has endpoints
  // (zoom in, out and reset, autofocus) that take none.
  function post(path, body) { return request(path, body, true); }

  // ---------- status ----------

  function setLive(kind, text) {
    var el = $('live');
    el.className = 'live live--' + kind;
    el.textContent = text;
  }

  function code(text) {
    var el = document.createElement('code');
    el.textContent = text;
    return el;
  }

  // `hint` is a list of strings and nodes. Built from nodes rather than HTML
  // because it can carry the net's name, which is whatever the user chose.
  function showEmpty(title, hint) {
    $('feed-empty-title').textContent = title;
    var el = $('feed-empty-hint');
    el.textContent = '';
    (hint || []).forEach(function (part) {
      el.append(part);
    });
    $('feed-empty').hidden = false;
  }

  function hideEmpty() { $('feed-empty').hidden = true; }

  function formatZoom(zoom) { return Number(zoom).toFixed(1) + '×'; }

  function applyInfo(info) {
    if (state.signedOut) return;
    if (!info) {
      setLive('off', 'OFFLINE');
      showEmpty('The stream does not answer.',
                ['It may have been stopped. Start it with ',
                 code('lager webcam ' + (state.net || 'NET') + ' start'), '.']);
      $('clock').hidden = true;
      return;
    }
    state.net = info.net;
    document.title = info.net + ' · Lager Webcam';
    $('model').textContent = info.model || info.net;
    $('device').textContent = info.video_device + ' · :' + info.port;
    $('stat-fps').textContent = info.fps ? info.fps.toFixed(1) + ' fps' : '— fps';
    $('stat-size').textContent = info.width ? info.width + '×' + info.height : '—';
    if (info.width && info.height) {
      $('frame').style.setProperty('--ratio', info.width + ' / ' + info.height);
    }
    $('stat-zoom').textContent = formatZoom(info.zoom);
    $('about-started').textContent = info.started_by
      ? (info.source ? info.source + ' · ' : '') + info.started_by
      : (info.source || '—');
    $('about-device').textContent = info.video_device;
    $('about-port').textContent = String(info.port);

    if (!state.dragging.zoom) {
      $('zoom').value = info.zoom;
      $('zoom-value').textContent = formatZoom(info.zoom);
    }
    state.zoom = info.zoom;
    showView(info.view || state.view);
    if (info.mode) showMode(info.mode, info.mode_error);

    if (info.frame_age === null || info.frame_age === undefined) {
      setLive('off', 'WAITING');
      showEmpty('Waiting for the first frame…');
      $('clock').hidden = true;
    } else if (info.frame_age > STALLED_AFTER_SECONDS) {
      setLive('stalled', 'STALLED');
      showEmpty('The camera stopped sending frames.',
                ['Last frame ' + Math.round(info.frame_age) + ' s ago. Check the camera’s cable.']);
      $('clock').hidden = true;
    } else {
      setLive('on', 'LIVE');
      hideEmpty();
      $('clock').hidden = false;
    }
  }

  function pollInfo() {
    if (state.signedOut) return;
    request('/api/info').then(applyInfo);
  }

  function tickClock() {
    $('clock').textContent = new Date().toLocaleTimeString([], { hour12: false });
  }

  // ---------- picture ----------

  // An MJPEG <img> that errors (the server restarted, the network dropped)
  // never recovers by itself. Reload it, backing off to once every 10 s.
  var retryDelay = 1000;
  function connectFeed() {
    if (state.signedOut) return;
    $('feed').src = '/stream?t=' + Date.now();
  }
  $('feed').addEventListener('load', function () { retryDelay = 1000; });
  $('feed').addEventListener('error', function () {
    if (state.signedOut) return;
    setTimeout(connectFeed, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 10000);
  });

  // ---------- zoom ----------

  function debounced(key, fn) {
    var timer = null;
    return function () {
      state.dragging[key] = true;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fn().then(function () { state.dragging[key] = false; });
      }, SLIDER_DEBOUNCE_MS);
    };
  }

  function showZoom(result) {
    if (result && result.zoom !== undefined) {
      state.zoom = result.zoom;
      showView(state.view);
      $('zoom').value = result.zoom;
      $('zoom-value').textContent = formatZoom(result.zoom);
      $('stat-zoom').textContent = formatZoom(result.zoom);
    }
  }

  $('zoom').addEventListener('input', function () {
    $('zoom-value').textContent = formatZoom($('zoom').value);
  });
  $('zoom').addEventListener('input', debounced('zoom', function () {
    return post('/api/zoom/set', { zoom: parseFloat($('zoom').value) }).then(showZoom);
  }));
  $('zoom-in').addEventListener('click', function () { post('/api/zoom/in').then(showZoom); });
  $('zoom-out').addEventListener('click', function () { post('/api/zoom/out').then(showZoom); });
  $('zoom-reset').addEventListener('click', function () { post('/api/zoom/reset').then(showZoom); });

  // ---------- focus ----------

  function showFocus(result) {
    if (!result || result.mode === undefined) return;
    var auto = result.mode === 'auto';
    $('focus-auto').setAttribute('aria-pressed', String(auto));
    $('focus-manual').setAttribute('aria-pressed', String(!auto));
    $('focus').disabled = auto;
    if (!state.dragging.focus && !auto) $('focus').value = result.value;
    $('focus-value').textContent = auto ? 'auto' : String($('focus').value);
  }

  $('focus-auto').addEventListener('click', function () {
    post('/api/focus/auto').then(showFocus);
  });
  $('focus-manual').addEventListener('click', function () {
    post('/api/focus/manual', { value: parseInt($('focus').value, 10) }).then(showFocus);
  });
  $('focus').addEventListener('input', function () {
    $('focus-value').textContent = $('focus').value;
  });
  $('focus').addEventListener('input', debounced('focus', function () {
    return post('/api/focus/manual', { value: parseInt($('focus').value, 10) }).then(showFocus);
  }));

  // ---------- brightness ----------

  function showBrightness(result) {
    if (!result || result.value === undefined || state.dragging.brightness) return;
    $('brightness').value = result.value;
    $('brightness-value').textContent = String(result.value);
  }

  $('brightness').addEventListener('input', function () {
    $('brightness-value').textContent = $('brightness').value;
  });
  $('brightness').addEventListener('input', debounced('brightness', function () {
    return post('/api/brightness/set', { value: parseInt($('brightness').value, 10) })
      .then(function (result) {
        state.dragging.brightness = false;
        showBrightness(result);
      });
  }));

  function pollSettings() {
    if (state.signedOut) return;
    request('/api/focus').then(showFocus);
    request('/api/brightness').then(showBrightness);
  }

  // ---------- pan and orientation ----------

  function showView(view) {
    state.view = view;
    var zoomed = state.zoom > 1.0;
    var centred = Math.abs(view.pan_x - 0.5) < 0.005 && Math.abs(view.pan_y - 0.5) < 0.005;
    $('frame').classList.toggle('is-pannable', zoomed);
    $('pan-centre').disabled = !zoomed || centred;
    $('pan-hint').textContent = zoomed
      ? 'Drag the picture, or use the arrow keys, to pan.'
      : 'Zoom in, then drag the picture to pan.';
    var chip = $('zoom-chip');
    chip.hidden = !zoomed;
    chip.textContent = formatZoom(state.zoom) + ' \u00b7 ' + (centred ? 'centre' : 'panned');
    $('rotate').value = String(view.rotate);
    $('flip-h').setAttribute('aria-pressed', String(!!view.flip_h));
    $('flip-v').setAttribute('aria-pressed', String(!!view.flip_v));
  }

  function setView(path, body) {
    return post(path, body).then(function (result) {
      if (result && result.view) showView(result.view);
    });
  }

  // Pan requests are coalesced: at most one in flight, the newest wins.
  var panPending = null;
  var panBusy = false;
  function panTo(x, y) {
    panPending = { x: Math.max(0, Math.min(1, x)), y: Math.max(0, Math.min(1, y)) };
    if (panBusy) return;
    panBusy = true;
    var send = function () {
      var target = panPending;
      panPending = null;
      setView('/api/pan/set', target).then(function () {
        if (panPending) send(); else panBusy = false;
      });
    };
    send();
  }

  // A point on the picture, as a fraction of the whole (unzoomed) frame.
  function framePoint(event) {
    var rect = $('feed').getBoundingClientRect();
    var fx = (event.clientX - rect.left) / rect.width;
    var fy = (event.clientY - rect.top) / rect.height;
    var span = 1 / state.zoom;
    return {
      x: state.view.pan_x - span / 2 + fx * span,
      y: state.view.pan_y - span / 2 + fy * span,
    };
  }

  var drag = null;
  $('frame').addEventListener('pointerdown', function (event) {
    if (state.zoom <= 1.0 || event.button !== 0) return;
    var rect = $('feed').getBoundingClientRect();
    drag = { x: event.clientX, y: event.clientY, w: rect.width, h: rect.height,
             panX: state.view.pan_x, panY: state.view.pan_y };
    $('frame').classList.add('is-panning');
    if ($('frame').setPointerCapture) $('frame').setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  $('frame').addEventListener('pointermove', function (event) {
    if (!drag) return;
    // Dragging right shows what is to the left: the pan moves against the hand.
    panTo(drag.panX - (event.clientX - drag.x) / (drag.w * state.zoom),
          drag.panY - (event.clientY - drag.y) / (drag.h * state.zoom));
  });
  function endDrag() {
    drag = null;
    $('frame').classList.remove('is-panning');
  }
  $('frame').addEventListener('pointerup', endDrag);
  $('frame').addEventListener('pointercancel', endDrag);

  // Double-click: zoom in on that point, or once zoomed, centre on it.
  $('frame').addEventListener('dblclick', function (event) {
    var point = framePoint(event);
    if (state.zoom <= 1.0) {
      post('/api/zoom/set', { zoom: 2.0 }).then(function (result) {
        showZoom(result);
        if (result) state.zoom = result.zoom;
        panTo(point.x, point.y);
      });
    } else {
      panTo(point.x, point.y);
    }
  });

  // Arrow keys pan while the picture has focus. On the element, not the
  // window, so they never steal keys from a slider or the Net picker.
  $('frame').addEventListener('keydown', function (event) {
    if (state.zoom <= 1.0) return;
    var step = PAN_KEY_STEP / state.zoom;
    var moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    var move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    panTo(state.view.pan_x + move[0], state.view.pan_y + move[1]);
  });

  $('pan-centre').addEventListener('click', function () { panTo(0.5, 0.5); });

  $('rotate').addEventListener('change', function () {
    setView('/api/orientation/set', { rotate: parseInt($('rotate').value, 10) });
  });
  $('flip-h').addEventListener('click', function () {
    setView('/api/orientation/set', { flip_h: !state.view.flip_h });
  });
  $('flip-v').addEventListener('click', function () {
    setView('/api/orientation/set', { flip_v: !state.view.flip_v });
  });

  // ---------- resolution ----------

  function sizeKey(mode) { return mode.width + 'x' + mode.height; }

  function fillFps(size, wanted) {
    var select = $('mode-fps');
    select.textContent = '';
    var match = state.modes.filter(function (m) { return sizeKey(m) === size; })[0];
    var rates = match ? match.fps : (state.mode ? [state.mode.fps] : []);
    rates.forEach(function (fps) {
      var option = document.createElement('option');
      option.value = String(fps);
      option.textContent = fps + ' fps';
      option.selected = String(fps) === String(wanted);
      select.appendChild(option);
    });
  }

  function fillModes() {
    var select = $('mode-size');
    select.textContent = '';
    var sizes = state.modes.slice();
    // A camera that lists nothing still shows the size it is streaming.
    if (state.mode && !sizes.some(function (m) { return sizeKey(m) === sizeKey(state.mode); })) {
      sizes.push({ width: state.mode.width, height: state.mode.height, fps: [state.mode.fps] });
    }
    sizes.forEach(function (m) {
      var option = document.createElement('option');
      option.value = sizeKey(m);
      option.textContent = m.width + '\u00d7' + m.height;
      option.selected = !!state.mode && sizeKey(m) === sizeKey(state.mode);
      select.appendChild(option);
    });
    select.disabled = sizes.length < 2;
    if (state.mode) fillFps(sizeKey(state.mode), state.mode.fps);
    $('mode-fps').disabled = $('mode-fps').children.length < 2;
  }

  function showMode(mode, error) {
    var changed = !state.mode || sizeKey(state.mode) !== sizeKey(mode) || state.mode.fps !== mode.fps;
    state.mode = mode;
    if (changed && !state.dragging.mode) fillModes();
    var note = $('mode-note');
    note.hidden = !error;
    note.textContent = error || '';
  }

  function requestMode(width, height, fps) {
    state.dragging.mode = true;
    var note = $('mode-note');
    note.hidden = false;
    note.textContent = 'Switching to ' + width + '\u00d7' + height + ' at ' + fps + ' fps\u2026';
    post('/api/mode/set', { width: width, height: height, fps: fps }).then(function (result) {
      state.dragging.mode = false;
      if (!result || !result.success) {
        note.textContent = (result && result.error) || 'The camera did not accept that mode.';
      }
    });
  }

  $('mode-size').addEventListener('change', function () {
    var parts = $('mode-size').value.split('x');
    var match = state.modes.filter(function (m) { return sizeKey(m) === $('mode-size').value; })[0];
    var fps = match ? match.fps[0] : (state.mode && state.mode.fps);
    fillFps($('mode-size').value, fps);
    requestMode(parseInt(parts[0], 10), parseInt(parts[1], 10), fps);
  });
  $('mode-fps').addEventListener('change', function () {
    var parts = $('mode-size').value.split('x');
    requestMode(parseInt(parts[0], 10), parseInt(parts[1], 10), Number($('mode-fps').value));
  });

  function loadModes() {
    request('/api/modes').then(function (data) {
      if (!data) return;
      state.modes = data.modes || [];
      state.mode = null;
      showMode(data.current, data.error);
    });
  }

  // ---------- exposure and gain ----------

  function showControls(data) {
    if (!data) return;
    var exposure = data.exposure;
    $('exposure-group').hidden = !exposure;
    if (exposure) {
      $('exposure-mode').hidden = exposure.auto === null || exposure.auto === undefined;
      var auto = exposure.auto === true;
      $('exposure-auto').setAttribute('aria-pressed', String(auto));
      $('exposure-manual').setAttribute('aria-pressed', String(!auto));
      var slider = $('exposure');
      slider.min = exposure.min;
      slider.max = exposure.max;
      slider.step = exposure.step || 1;
      slider.disabled = auto;
      if (!state.dragging.exposure) slider.value = exposure.value;
      $('exposure-value').textContent = auto ? 'auto' : String(slider.value);
    }
    var gain = data.gain;
    $('gain-group').hidden = !gain;
    if (gain) {
      // Under auto exposure the camera sets gain itself and ignores what is
      // written, so the slider is held, as Distance is under auto focus.
      var gainAuto = !!(exposure && exposure.auto === true);
      $('gain').min = gain.min;
      $('gain').max = gain.max;
      $('gain').step = gain.step || 1;
      $('gain').disabled = gainAuto;
      if (!state.dragging.gain) $('gain').value = gain.value;
      $('gain-value').textContent = gainAuto ? 'auto' : String($('gain').value);
    }
  }

  function setControl(control, value) {
    return post('/api/controls/set', { control: control, value: value }).then(function (result) {
      if (result && result.controls) showControls(result.controls);
    });
  }

  $('exposure-auto').addEventListener('click', function () { setControl('exposure_auto', true); });
  $('exposure-manual').addEventListener('click', function () { setControl('exposure_auto', false); });
  $('exposure').addEventListener('input', function () {
    $('exposure-value').textContent = $('exposure').value;
  });
  $('exposure').addEventListener('input', debounced('exposure', function () {
    return setControl('exposure', parseInt($('exposure').value, 10));
  }));
  $('gain').addEventListener('input', function () {
    $('gain-value').textContent = $('gain').value;
  });
  $('gain').addEventListener('input', debounced('gain', function () {
    return setControl('gain', parseInt($('gain').value, 10));
  }));

  function pollControls() {
    if (state.signedOut) return;
    request('/api/controls').then(showControls);
  }

  // ---------- full screen ----------

  $('btn-fullscreen').addEventListener('click', function () {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if ($('feed-section').requestFullscreen) {
      $('feed-section').requestFullscreen();
    }
  });
  document.addEventListener('fullscreenchange', function () {
    var on = !!document.fullscreenElement;
    $('btn-fullscreen').textContent = on ? 'Exit full screen' : 'Full screen';
    $('btn-fullscreen').title = on ? 'Leave full screen' : 'Show the picture full screen';
  });

  // ---------- other streams ----------

  // Each stream is served on its own port. Switching nets moves the page to
  // that port on the host the browser already reached the box at.
  function pollStreams() {
    if (state.signedOut) return;
    request('/api/streams').then(function (data) {
      if (!data || !data.streams) return;
      var select = $('net-select');
      var current = data.current;
      var wanted = data.streams.map(function (s) { return s.name + ':' + s.port; }).join(',');
      if (select.dataset.streams === wanted) return;
      select.dataset.streams = wanted;
      select.textContent = '';
      data.streams.forEach(function (stream) {
        var option = document.createElement('option');
        option.value = String(stream.port);
        option.textContent = stream.name;
        option.selected = stream.name === current;
        select.appendChild(option);
      });
    });
  }

  // Only a port number moves the page, and only to that port on this host:
  // the value comes from the box, and pasted into a string, something like
  // "80@other.host" would send the browser somewhere else entirely.
  function portUrl(value) {
    if (!/^[0-9]{1,5}$/.test(value)) return null;
    var port = parseInt(value, 10);
    if (port < 1 || port > 65535) return null;
    var url = new URL(window.location.href);
    url.port = String(port);
    url.pathname = '/';
    url.search = '';
    url.hash = '';
    return url.href;
  }

  $('net-select').addEventListener('change', function () {
    var target = portUrl($('net-select').value);
    if (target) window.location.assign(target);
  });

  // ---------- snapshot ----------

  function stamp() {
    var d = new Date();
    function two(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + two(d.getMonth() + 1) + two(d.getDate()) + '-' +
      two(d.getHours()) + two(d.getMinutes()) + two(d.getSeconds());
  }

  $('btn-snapshot').addEventListener('click', function () {
    var link = $('btn-snapshot');
    link.href = '/snapshot?t=' + Date.now();
    link.download = (state.net || 'webcam') + '-' + stamp() + '.jpg';
  });

  // ---------- sidebar ----------

  $('btn-sidebar').addEventListener('click', function () {
    var root = document.documentElement;
    var hidden = root.classList.toggle(SIDEBAR_HIDDEN_CLASS);
    var button = $('btn-sidebar');
    button.setAttribute('aria-expanded', String(!hidden));
    button.textContent = hidden ? 'Show sidebar' : 'Hide sidebar';
    button.title = hidden ? 'Show the controls sidebar' : 'Hide the controls sidebar';
    try {
      localStorage.setItem(SIDEBAR_STORAGE_KEY, hidden ? 'hidden' : 'shown');
    } catch (e) { /* storage blocked: the choice lasts this visit only */ }
  });

  // ---------- start ----------

  connectFeed();
  pollInfo();
  pollSettings();
  pollStreams();
  pollControls();
  loadModes();
  tickClock();
  setInterval(pollInfo, INFO_POLL_MS);
  setInterval(pollSettings, SETTINGS_POLL_MS);
  setInterval(pollStreams, STREAMS_POLL_MS);
  setInterval(pollControls, CONTROLS_POLL_MS);
  setInterval(tickClock, 1000);
})();
