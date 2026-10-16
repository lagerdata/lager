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
 *   POST /api/zoom/set|in|out|reset, /api/focus/auto|manual, /api/brightness/set
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
  // Light debounce for sliders: snappy, without a request per pixel.
  var SLIDER_DEBOUNCE_MS = 75;

  function $(id) { return document.getElementById(id); }

  var state = {
    net: null,
    signedOut: false,
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
  function request(path, body) {
    var options = { cache: 'no-store', credentials: 'same-origin' };
    if (body !== undefined) {
      options.method = 'POST';
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

  function post(path, body) { return request(path, body === undefined ? {} : body); }

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
    var zoomChip = $('zoom-chip');
    zoomChip.hidden = info.zoom <= 1.0;
    zoomChip.textContent = formatZoom(info.zoom) + ' · centre';

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

  $('net-select').addEventListener('change', function () {
    var port = $('net-select').value;
    window.location.href = window.location.protocol + '//' + window.location.hostname + ':' + port + '/';
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
  tickClock();
  setInterval(pollInfo, INFO_POLL_MS);
  setInterval(pollSettings, SETTINGS_POLL_MS);
  setInterval(pollStreams, STREAMS_POLL_MS);
  setInterval(tickClock, 1000);
})();
