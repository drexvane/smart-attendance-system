/**
 * CSRF token propagation.
 *
 * The camera-control and attendance-marking endpoints used to be CSRF-exempt, which
 * meant any external page could start this machine's camera or mark attendance with a
 * cross-site request.  They are protected now, so every state-changing fetch/XHR has
 * to carry the token.
 *
 * This wraps window.fetch and jQuery's ajax so existing call sites keep working
 * unchanged -- no per-call edits were needed.
 */
(function () {
  'use strict';

  const meta = document.querySelector('meta[name="csrf-token"]');
  const token = meta ? meta.getAttribute('content') : '';

  const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS', 'TRACE']);

  function isSameOrigin(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (err) {
      return false;
    }
  }

  // --- fetch ---------------------------------------------------------------
  const originalFetch = window.fetch;
  if (typeof originalFetch === 'function') {
    window.fetch = function (resource, init) {
      const options = Object.assign({}, init);
      const method = (options.method || (resource && resource.method) || 'GET').toUpperCase();
      const url = typeof resource === 'string' ? resource : (resource && resource.url) || '';

      if (token && !SAFE_METHODS.has(method) && isSameOrigin(url)) {
        const headers = new Headers(options.headers || (resource && resource.headers) || {});
        if (!headers.has('X-CSRFToken')) {
          headers.set('X-CSRFToken', token);
        }
        options.headers = headers;
        if (!options.credentials) {
          options.credentials = 'same-origin';
        }
      }
      return originalFetch.call(this, resource, options);
    };
  }

  // --- jQuery --------------------------------------------------------------
  if (window.jQuery) {
    window.jQuery.ajaxSetup({
      beforeSend: function (xhr, settings) {
        const method = (settings.type || 'GET').toUpperCase();
        if (token && !SAFE_METHODS.has(method) && isSameOrigin(settings.url || '')) {
          xhr.setRequestHeader('X-CSRFToken', token);
        }
      },
    });
  }

  // Exposed for code that builds requests by hand.
  window.CSRF_TOKEN = token;
})();
