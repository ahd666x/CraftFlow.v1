/* csrf.js — سلوی چوب
   CSRF token handling and AJAX helper for Django.

   Exposes:
     getCsrfToken()   — returns the CSRF token string
     App.postJSON(url, data, options)
     App.fetchJSON(url, options)
     App.showLoading(on)
     App.showToast(message, type)
*/

(function (window, document) {
    'use strict';

    var csrftoken = (function () {
        // Try cookie first
        function getCookie(name) {
            var value = "; " + document.cookie;
            var parts = value.split("; " + name + "=");
            if (parts.length === 2) {
                return parts.pop().split(";").shift();
            }
            return '';
        }

        // Try meta tag
        var meta = document.querySelector('meta[name="csrf-token"]');
        if (meta && meta.getAttribute('content') && meta.getAttribute('content') !== 'NOTPROVIDED') {
            return meta.getAttribute('content');
        }

        // Fallback to cookie
        var token = getCookie('csrftoken');
        if (token) return token;

        console.warn('CSRF token not found. Did you forget {% csrf_token %}?');
        return '';
    })();

    window.getCsrfToken = function () {
        return csrftoken;
    };

    // ── Default AJAX headers ──────────────────────────────────────────
    function defaultHeaders(extra) {
        var headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRFToken': csrftoken,
            'Content-Type': 'application/json',
        };
        if (extra) {
            Object.keys(extra).forEach(function (k) { headers[k] = extra[k]; });
        }
        return headers;
    }

    // ── Loading overlay ───────────────────────────────────────────────
    function ensureLoadingOverlay() {
        var el = document.getElementById('loading-overlay');
        if (!el) {
            el = document.createElement('div');
            el.id = 'loading-overlay';
            el.className = 'loading-overlay';
            el.innerHTML = '<div class="loading-spinner"></div>';
            document.body.appendChild(el);
        }
        return el;
    }

    function showLoading(on) {
        var el = ensureLoadingOverlay();
        el.style.display = on ? 'flex' : 'none';
    }

    // ── Toast notifications ───────────────────────────────────────────
    function ensureToastContainer() {
        var container = document.getElementById('toast-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toast-container';
            document.body.appendChild(container);
        }
        return container;
    }

    function showToast(message, type) {
        type = type || 'info';
        var container = ensureToastContainer();

        var toast = document.createElement('div');
        toast.className = 'toast align-items-center';
        var bgClass = type === 'error' ? 'bg-danger' : type === 'success' ? 'bg-success' : type === 'warning' ? 'bg-warning text-dark' : 'bg-info text-dark';

        toast.innerHTML =
            '<div class="d-flex">' +
                '<div class="toast-body">' + message + '</div>' +
                '<button type="button" class="btn-close btn-close-white me-auto m-2" aria-label="بستن"></button>' +
            '</div>';

        // Auto-dismiss after 4s
        var dismissTimer = setTimeout(function () {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }, 4000);

        toast.addEventListener('click', function (e) {
            if (e.target.classList.contains('btn-close')) {
                clearTimeout(dismissTimer);
                toast.parentNode.removeChild(toast);
            }
        });

        container.appendChild(toast);
    }

    // ── App namespace ─────────────────────────────────────────────────
    window.App = {
        postJSON: function (url, data, options) {
            options = options || {};
            var fetchOptions = {
                method: 'POST',
                headers: defaultHeaders(options.headers),
                credentials: 'same-origin',
            };
            if (data !== undefined && data !== null) {
                fetchOptions.body = JSON.stringify(data);
            }

            var doFetch = function () {
                return fetch(url, fetchOptions).then(function (response) {
                    var contentType = response.headers.get('content-type') || '';
                    if (contentType.indexOf('application/json') !== -1) {
                        return response.json().then(function (json) {
                            return { response: response, data: json };
                        });
                    }
                    return { response: response, data: null };
                });
            };

            if (options.loading !== false) {
                showLoading(true);
                return doFetch().then(function (result) {
                    showLoading(false);
                    if (!result.response.ok) {
                        var msg = result.data && result.data.error ? result.data.error : ('خطا در ارتباط با سرور (' + result.response.status + ')');
                        showToast(msg, 'error');
                        throw new Error(msg);
                    }
                    if (result.data && result.data.message) {
                        showToast(result.data.message, 'success');
                    }
                    if (options.onSuccess) options.onSuccess(result.data);
                    return result.data;
                }).catch(function (err) {
                    showLoading(false);
                    if (options.onError) {
                        options.onError(err);
                    } else {
                        showToast(err.message || 'خطای ناشناخته', 'error');
                    }
                    throw err;
                });
            }

            return doFetch();
        },

        fetchJSON: function (url, options) {
            options = options || {};
            var method = options.method || 'GET';
            var fetchOptions = {
                method: method,
                headers: defaultHeaders(options.headers),
                credentials: 'same-origin',
            };
            if (options.params) {
                url += (url.indexOf('?') === -1 ? '?' : '&') + new URLSearchParams(options.params).toString();
            }

            var doFetch = function () {
                return fetch(url, fetchOptions).then(function (response) {
                    var contentType = response.headers.get('content-type') || '';
                    if (contentType.indexOf('application/json') !== -1) {
                        return response.json().then(function (json) {
                            return { response: response, data: json };
                        });
                    }
                    return { response: response, data: null };
                });
            };

            if (options.loading !== false) {
                showLoading(true);
                return doFetch().then(function (result) {
                    showLoading(false);
                    if (!result.response.ok) {
                        var msg = result.data && result.data.error ? result.data.error : ('خطا در ارتباط با سرور (' + result.response.status + ')');
                        showToast(msg, 'error');
                        throw new Error(msg);
                    }
                    if (options.onSuccess) options.onSuccess(result.data);
                    return result.data;
                }).catch(function (err) {
                    showLoading(false);
                    if (options.onError) {
                        options.onError(err);
                    } else {
                        showToast(err.message || 'خطای ناشناخته', 'error');
                    }
                    throw err;
                });
            }

            return doFetch();
        },

        showLoading: showLoading,
        showToast: showToast,
        getCSRFToken: window.getCsrfToken,
    };

})(window, document);
