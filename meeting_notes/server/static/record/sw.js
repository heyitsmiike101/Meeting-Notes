/* Meeting Notes recorder service worker. Caches ONLY the recorder shell so /record/ opens with no connection after it
   has been opened online once. It never touches /v1/, sign-in pages, redirects or anything that is not a plain
   same-origin 200 response. __VERSION__ and __SHELL__ are filled in by the server. */
'use strict';
var VERSION = '__VERSION__';
var CACHE = 'mn-record-' + VERSION;
var PAGE = '/record/';
var SHELL = __SHELL__;

function cacheable(res) {
  return !!res && res.ok && res.status === 200 && res.type === 'basic' && !res.redirected;
}
function never(url) {
  return url.origin !== self.location.origin || url.pathname.indexOf('/v1/') === 0 ||
    url.pathname === '/login' || url.pathname === '/logout';
}

self.addEventListener('install', function (event) {
  event.waitUntil(caches.open(CACHE).then(function (cache) {
    return Promise.all(SHELL.map(function (u) {
      return fetch(u, {credentials: 'same-origin', cache: 'reload'}).then(function (res) {
        if (cacheable(res)) return cache.put(u, res);
      }).catch(function () { /* offline install of a page already cached is fine */ });
    }));
  }));
});

self.addEventListener('activate', function (event) {
  event.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k.indexOf('mn-record-') === 0 && k !== CACHE; })
      .map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener('message', function (event) {
  if (event.data === 'skip') self.skipWaiting();
});

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (never(url)) return;
  if (req.mode === 'navigate' && (url.pathname === '/record/' || url.pathname === '/record')) {
    // network first, the cached page only when the network is unreachable
    event.respondWith(fetch(req).then(function (res) {
      if (cacheable(res)) { var copy = res.clone(); caches.open(CACHE).then(function (c) { c.put(PAGE, copy); }); }
      return res;
    }).catch(function () {
      return caches.match(PAGE).then(function (hit) { return hit || Response.error(); });
    }));
    return;
  }
  var key = url.pathname + url.search;
  if (SHELL.indexOf(key) === -1) return;
  event.respondWith(caches.match(key).then(function (hit) {
    if (hit) return hit;
    return fetch(req).then(function (res) {
      if (cacheable(res)) { var copy = res.clone(); caches.open(CACHE).then(function (c) { c.put(key, copy); }); }
      return res;
    });
  }));
});
