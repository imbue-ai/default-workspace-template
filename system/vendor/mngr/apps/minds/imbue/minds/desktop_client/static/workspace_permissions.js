// The Permissions pane of the workspace options panel: left-nav switching plus
// the toggle / revoke-all / connect actions.
//
// Two pages load it: the overlay-hosted panel (pages.WorkspaceOptionsModal,
// Electron) and its full-page twin (pages.WorkspaceOptions, browser mode).
// Everything is guarded on #ws-permissions so surfaces without the pane (or a
// pane rendered in its gateway-unavailable state) load the file as a no-op.
//
// A toggle click POSTs ONLY the flipped permission and its new state; the
// server recomputes the affected rule's complete permission set from the
// current file and writes that back (see latchkey/permission_toggles.py).
// The flip is optimistic -- aria-checked switches immediately -- and reverts
// with an error line if the POST fails, so the control never lies about what
// is stored for longer than one round trip.

(function () {
  'use strict';

  // -- Waiting on you (pending-request rows above the toggle panels) --------
  //
  // Bound outside the #ws-permissions guard: the strip renders even when the
  // gateway is unavailable and the toggle panels are replaced by a notice.
  // A row opens the shared review popup on its request; in Electron that is
  // the modal overlay (replacing this options panel), in browser mode a
  // navigation to the popup page.
  Array.prototype.forEach.call(document.querySelectorAll('.perm-waiting-row'), function (row) {
    row.addEventListener('click', function () {
      var requestId = row.dataset.requestId;
      if (!requestId) return;
      if (window.minds && window.minds.openRequestPopup) {
        // Electron: opens the popup stacked over this panel; closing the
        // popup restores the panel (the displaced-modal detour).
        window.minds.openRequestPopup(requestId);
      } else {
        // Browser mode: full-page popup; return_to brings the user back to
        // this Permissions page when the popup closes.
        var returnTo = window.location.pathname + window.location.search;
        window.location.href = '/inbox?selected=' + encodeURIComponent(requestId)
          + '&return_to=' + encodeURIComponent(returnTo);
      }
    });
  });

  // "+N more" reveals the folded rows and retires itself.
  var waitingMore = document.getElementById('ws-perm-waiting-more');
  if (waitingMore) {
    waitingMore.addEventListener('click', function () {
      Array.prototype.forEach.call(document.querySelectorAll('.perm-waiting-row.hidden'), function (row) {
        row.classList.remove('hidden');
      });
      waitingMore.classList.add('hidden');
    });
  }

  var root = document.getElementById('ws-permissions');
  if (!root) return;
  var agentId = root.dataset.agentId;

  function el(id) { return document.getElementById(id); }

  function showError(message) {
    var errorEl = el('ws-perm-error');
    if (!errorEl) return;
    errorEl.textContent = message;
    errorEl.classList.remove('hidden');
  }

  function clearError() {
    var errorEl = el('ws-perm-error');
    if (errorEl) errorEl.classList.add('hidden');
  }

  // Same contract as workspace_options.js: 4xx/5xx responses become rejected
  // promises carrying the server's error message.
  function requestWithErrorCheck(url, options) {
    return fetch(url, options).then(function (response) {
      if (response.ok) return response;
      return response.text().then(function (text) {
        var detail = text;
        try {
          detail = window.normalizeApiError(JSON.parse(text)).message;
        } catch (_) { /* leave detail as the raw body */ }
        throw new Error(detail || ('HTTP ' + response.status));
      });
    });
  }

  function postJson(path, body) {
    return requestWithErrorCheck('/workspace/' + encodeURIComponent(agentId) + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  // -- Left nav --------------------------------------------------------------

  function selectSection(key) {
    Array.prototype.forEach.call(root.querySelectorAll('[data-perm-nav]'), function (button) {
      var isSelected = button.dataset.permNav === key;
      button.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
      button.classList.toggle('bg-fill-hover', isSelected);
      button.classList.toggle('font-semibold', isSelected);
    });
    Array.prototype.forEach.call(root.querySelectorAll('[data-perm-panel]'), function (panel) {
      panel.classList.toggle('hidden', panel.dataset.permPanel !== key);
    });
    clearError();
  }

  Array.prototype.forEach.call(root.querySelectorAll('[data-perm-nav]'), function (button) {
    button.addEventListener('click', function () { selectSection(button.dataset.permNav); });
  });

  // -- Permission toggles ----------------------------------------------------

  function setSwitch(switchEl, isOn) {
    switchEl.setAttribute('aria-checked', isOn ? 'true' : 'false');
  }

  function isSwitchOn(switchEl) {
    return switchEl.getAttribute('aria-checked') === 'true';
  }

  function onSwitchClick(switchEl) {
    // One in-flight write per control: a second click while busy is ignored
    // rather than queued, so the stored state never races itself.
    if (switchEl.disabled || switchEl.classList.contains('is-busy')) return;
    clearError();
    var turnOn = !isSwitchOn(switchEl);
    var body = { permission: switchEl.dataset.permPermission, enabled: turnOn };
    var path = '/permissions/self-toggle';
    if (switchEl.dataset.permKind === 'connector') {
      path = '/permissions/connector-toggle';
      body.scope = switchEl.dataset.permScope;
      // The unnamed default account is the empty string, which is a valid
      // account key -- send it verbatim rather than dropping falsy values.
      body.account = switchEl.dataset.permAccount || '';
    }
    setSwitch(switchEl, turnOn);
    switchEl.classList.add('is-busy');
    postJson(path, body)
      .then(function () {
        switchEl.classList.remove('is-busy');
      })
      .catch(function (error) {
        switchEl.classList.remove('is-busy');
        setSwitch(switchEl, !turnOn);
        showError('Could not save the change: ' + error.message);
      });
  }

  Array.prototype.forEach.call(root.querySelectorAll('.perm-switch'), function (switchEl) {
    switchEl.addEventListener('click', function () { onSwitchClick(switchEl); });
  });

  // -- Revoke all (per connection) ------------------------------------------
  //
  // Two-step confirm on the button itself (no modal plumbing in the overlay):
  // the first click arms it and relabels it, the second click within the
  // window fires the revoke, and the label falls back after a beat.

  var CONFIRM_WINDOW_MS = 4000;

  Array.prototype.forEach.call(root.querySelectorAll('.perm-revoke-all-btn'), function (button) {
    var originalLabel = button.textContent;
    var disarmTimer = null;

    function disarm() {
      if (disarmTimer !== null) {
        clearTimeout(disarmTimer);
        disarmTimer = null;
      }
      button.dataset.armed = '';
      button.textContent = originalLabel;
    }

    button.addEventListener('click', function () {
      clearError();
      if (button.dataset.armed !== 'true') {
        button.dataset.armed = 'true';
        button.textContent = 'Really revoke all?';
        disarmTimer = setTimeout(disarm, CONFIRM_WINDOW_MS);
        return;
      }
      disarm();
      button.disabled = true;
      postJson('/permissions/connector-revoke-all', {
        service_name: button.dataset.serviceName,
        account: button.dataset.account || '',
      })
        .then(function () {
          window.location.reload();
        })
        .catch(function (error) {
          button.disabled = false;
          showError('Could not revoke ' + (button.dataset.serviceLabel || 'the service') + ': ' + error.message);
        });
    });
  });

  // -- Add connection --------------------------------------------------------
  //
  // Connect runs the same synchronous ephemeral-browser sign-in the settings
  // page's "+ Add account" uses (the route blocks until the browser flow
  // finishes), then reloads so the new connection renders with its toggles.

  Array.prototype.forEach.call(root.querySelectorAll('.perm-connect-btn'), function (button) {
    button.addEventListener('click', function () {
      clearError();
      var busy = window.mindsButtonBusy;
      if (busy) busy.set(button, 'Waiting for sign-in...');
      button.disabled = true;
      requestWithErrorCheck('/settings/connectors/add-account', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ service_name: button.dataset.serviceName }),
      })
        .then(function () {
          window.location.reload();
        })
        .catch(function (error) {
          if (busy) busy.clear(button);
          button.disabled = false;
          showError('Could not connect: ' + error.message);
        });
    });
  });
})();
