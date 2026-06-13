const CACHE_NAME = 'ecolyxis-v11';
const STATIC_ASSETS = [
    '/static/css/style.css',
    '/static/css/billing.css',
    '/static/manifest.json',
    '/static/img/icon-192.png',
    '/static/img/icon-512.png',
    '/static/js/dashboard.js',
    '/static/js/webauthn.js',
    '/static/js/marked.min.js',
    '/static/js/purify.min.js',
];

// Offline fallback page (inline data URI so no extra file needed)
const OFFLINE_BODY = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Offline — Ecolyxis</title>
<style>
body{background:#0d1117;color:#c9d1d9;font-family:system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{text-align:center;max-width:400px;padding:2rem}
.card svg{width:64px;height:64px;margin-bottom:1rem;opacity:.5}
.card h1{font-size:1.25rem;margin:0 0 .5rem}
.card p{font-size:.9rem;color:#8b949e;line-height:1.5;margin:0 0 1.5rem}
.card button{background:#2ecc71;color:#fff;border:none;padding:.6rem 1.5rem;
border-radius:8px;font-size:.9rem;cursor:pointer}
</style>
</head>
<body>
<div class="card">
<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
<path d="M1 1l22 22M16.72 11.06A10.94 10.94 0 0 1 19 12.55M5 12.55a10.94 10.94 0 0 1 5.17-2.39M10.71 5.05A16 16 0 0 1 22.58 9M1.42 9a15.91 15.91 0 0 1 4.7-2.88M8.53 16.11a6 6 0 0 1 6.95 0M12 20h.01"/>
</svg>
<h1>You're Offline</h1>
<p>Ecolyxis needs an internet connection. Check your network and try again.</p>
<button onclick="location.reload()">Retry</button>
</div>
</body>
</html>`;

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(STATIC_ASSETS))
            .catch(() => {})
    );
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
        )
    );
    self.clients.claim();
});

// Stale-while-revalidate for static assets
function staleWhileRevalidate(event) {
    return caches.match(event.request).then(cached => {
        const fetchPromise = fetch(event.request).then(response => {
            if (response.ok) {
                const clone = response.clone();
                caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
            }
            return response;
        }).catch(() => cached);
        return cached || fetchPromise;
    });
}

self.addEventListener('fetch', event => {
    if (event.request.method !== 'GET') return;

    // Navigation requests: network-first with offline fallback
    if (event.request.mode === 'navigate') {
        event.respondWith(
            fetch(event.request).catch(() => {
                return caches.match(event.request).then(cached => {
                    return cached || new Response(OFFLINE_BODY, {
                        headers: { 'Content-Type': 'text/html' }
                    });
                });
            })
        );
        return;
    }

    // Static assets: stale-while-revalidate
    if (event.request.url.includes('/static/')) {
        event.respondWith(staleWhileRevalidate(event));
        return;
    }

    // Everything else: network-first, fall back to cache
    event.respondWith(
        fetch(event.request).catch(() => caches.match(event.request))
    );
});
