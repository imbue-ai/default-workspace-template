// The notifications page (README, "Notifications on a phone"): turns Web Push on or off for the device it is opened
// on, sends that device a test, lists every device that has it on, and gives the owner the full-text switch, the log
// of recent sends, and the reset. Every URL is relative to the page, so the page works wherever the shell's root is.

const statusElement = document.getElementById("status");
const turnOnButton = document.getElementById("turn-on");
const sendTestButton = document.getElementById("send-test");
const turnOffButton = document.getElementById("turn-off");
const devicesList = document.getElementById("devices");
const noDevicesNote = document.getElementById("no-devices");
const noDevicesText = noDevicesNote.textContent;
const lastAnswer = document.getElementById("last-answer");
const lastAnswerBody = document.getElementById("last-answer-body");
const desktopNote = document.getElementById("desktop-note");
const ownerControls = document.getElementById("owner-controls");
const fullTextBox = document.getElementById("full-text");
const sendsList = document.getElementById("sends");
const noSendsNote = document.getElementById("no-sends");
const resetButton = document.getElementById("reset");
const resetConfirmButton = document.getElementById("reset-confirm");
const resetCancelButton = document.getElementById("reset-cancel");
const turnOnLabel = turnOnButton.textContent;

const isSupported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const isAppleMobile =
  /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isStandalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
// A phone or a tablet: its main pointer is a finger. Elsewhere the Imbue Studio app already notifies the owner, so this
// page asks for an explicit choice before turning a browser on.
const isHandheld = window.matchMedia("(pointer: coarse)").matches;

let registration = null;
let publicKey = null;
let thisDeviceId = null;

function showStatus(text, tone = "neutral") {
  statusElement.textContent = text;
  statusElement.dataset.tone = tone;
}

function showAnswer(answer) {
  lastAnswer.hidden = false;
  lastAnswerBody.textContent = JSON.stringify(answer, null, 2);
}

// What the workspace refused, with its HTTP status.
class WorkspaceAnswerError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

const HTTP_FORBIDDEN = 403;

async function requestJson(method, path, body) {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let parsed = null;
  let isReadable = true;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch (error) {
    // A proxy's own error page, say, rather than the workspace's JSON.
    console.warn(`${method} ${path} answered ${response.status} with a body that is not JSON`, error);
    isReadable = false;
  }
  if (!response.ok) {
    throw new WorkspaceAnswerError(
      parsed && parsed.detail ? parsed.detail : `The workspace answered ${response.status}`,
      response.status,
    );
  }
  if (!isReadable) throw new WorkspaceAnswerError("The workspace's answer is not readable", response.status);
  return parsed;
}

// The key as the bytes ``PushManager.subscribe`` takes: Safari's older releases accept no string form.
function keyBytes(base64url) {
  const base64 = base64url.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(base64url.length / 4) * 4, "=");
  return Uint8Array.from(atob(base64), (character) => character.charCodeAt(0));
}

// The id the workspace lists this device under: the start of the SHA-256 of its push endpoint.
async function deviceIdOf(subscription) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(subscription.endpoint));
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 16);
}

// What to call this device in the list, from what its browser says about itself.
function deviceLabel() {
  const agent = navigator.userAgent;
  const system = isAppleMobile
    ? /iPad/.test(agent) || navigator.platform === "MacIntel"
      ? "iPad"
      : "iPhone"
    : /Android/.test(agent)
      ? "Android"
      : /Mac OS X/.test(agent)
        ? "Mac"
        : /Windows/.test(agent)
          ? "Windows"
          : /Linux/.test(agent)
            ? "Linux"
            : "Device";
  const browser = /Firefox\//.test(agent)
    ? "Firefox"
    : /Edg\//.test(agent)
      ? "Edge"
      : /Chrome\//.test(agent)
        ? "Chrome"
        : /Safari\//.test(agent)
          ? "Safari"
          : "browser";
  return `${system}, ${browser}${isStandalone ? " (Home Screen app)" : ""}`;
}

function renderDevices(devices) {
  devicesList.replaceChildren();
  noDevicesNote.textContent = noDevicesText;
  noDevicesNote.hidden = devices.length > 0;
  for (const device of devices) {
    const item = document.createElement("li");
    const description = document.createElement("span");
    description.textContent = device.device_id === thisDeviceId ? `${device.label} (this device)` : device.label;
    const detail = document.createElement("small");
    const delivered = device.last_accepted_at
      ? `last delivered ${new Date(device.last_accepted_at).toLocaleString()}`
      : "nothing delivered yet";
    detail.textContent = `${device.push_provider}, since ${new Date(device.registered_at).toLocaleString()}, ${delivered}`;
    description.append(detail);
    if (device.is_stale) {
      const stale = document.createElement("small");
      stale.className = "stale";
      stale.textContent = `Not reaching it (its push service answered ${device.last_status_code}): turn notifications on again on that device.`;
      description.append(stale);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => forgetDevice(device.device_id));
    item.append(description, remove);
    devicesList.append(item);
  }
}

// The devices with notifications on; a refusal (the requester is not the owner, who alone registers) is thrown.
async function refreshDevices() {
  try {
    const listing = await requestJson("GET", "api/web-push/devices");
    renderDevices(listing.devices);
    return listing.devices;
  } catch (error) {
    if (error.status === HTTP_FORBIDDEN) throw error;
    noDevicesNote.hidden = false;
    noDevicesNote.textContent = error.message;
    return [];
  }
}

async function currentSubscription() {
  return registration ? registration.pushManager.getSubscription() : null;
}

function showButtons({ isOn }) {
  turnOnButton.hidden = isOn;
  sendTestButton.hidden = !isOn;
  turnOffButton.hidden = !isOn;
}

// The owner's controls, which work whether or not this browser can get notifications; a visitor's request is refused
// and they stay hidden.
async function showOwnerControls() {
  try {
    await refreshDevices();
  } catch (error) {
    console.warn("Not showing the owner's controls", error);
    return;
  }
  ownerControls.hidden = false;
  await Promise.all([refreshSettings(), refreshSends()]);
}

async function render() {
  if (!isSupported) {
    showButtons({ isOn: false });
    turnOnButton.hidden = true;
    if (isAppleMobile && !isStandalone) {
      showStatus(
        "On iPhone and iPad, notifications work only in the workspace's Home Screen app (iOS 16.4 or later). " +
          "Add this workspace to your Home Screen, open it from there, and come back to this page.",
        "bad",
      );
    } else {
      showStatus("This browser cannot show notifications from a web page.", "bad");
    }
    await showOwnerControls();
    return;
  }
  if (Notification.permission === "denied") {
    showButtons({ isOn: false });
    turnOnButton.hidden = true;
    showStatus(
      "Notifications are blocked for this workspace. Allow them in this browser's or this device's settings, " +
        "then reload this page.",
      "bad",
    );
    await showOwnerControls();
    return;
  }
  const subscription = Notification.permission === "granted" ? await currentSubscription() : null;
  thisDeviceId = subscription ? await deviceIdOf(subscription) : null;
  let devices;
  try {
    devices = await refreshDevices();
  } catch (error) {
    showButtons({ isOn: false });
    turnOnButton.hidden = true;
    noDevicesNote.hidden = true;
    showStatus(error.message, "bad");
    return;
  }
  ownerControls.hidden = false;
  await Promise.all([refreshSettings(), refreshSends()]);
  const thisDevice = thisDeviceId === null ? undefined : devices.find((device) => device.device_id === thisDeviceId);
  const isRegistered = thisDevice !== undefined && !thisDevice.is_stale;
  showButtons({ isOn: isRegistered });
  desktopNote.hidden = isRegistered || isHandheld;
  turnOnButton.textContent = isRegistered || isHandheld ? turnOnLabel : "Turn on in this browser anyway";
  if (thisDevice !== undefined && thisDevice.is_stale) {
    showStatus(
      "Notifications stopped reaching this device: its push service refused this workspace's key. Turn them on again.",
      "bad",
    );
    return;
  }
  showStatus(
    isRegistered ? "Notifications are on for this device." : "Notifications are off for this device.",
    isRegistered ? "good" : "neutral",
  );
}

async function refreshSettings() {
  try {
    const settings = await requestJson("GET", "api/web-push/settings");
    fullTextBox.checked = settings.is_full_text;
    fullTextBox.disabled = false;
  } catch (error) {
    fullTextBox.disabled = true;
    console.warn("Could not read the notification settings", error);
  }
}

async function saveFullText() {
  fullTextBox.disabled = true;
  try {
    const settings = await requestJson("POST", "api/web-push/settings", { is_full_text: fullTextBox.checked });
    fullTextBox.checked = settings.is_full_text;
  } catch (error) {
    fullTextBox.checked = !fullTextBox.checked;
    showStatus(`Could not save the setting: ${error.message}`, "bad");
  } finally {
    fullTextBox.disabled = false;
  }
}

const SEND_STATUS_TEXT = {
  queued: "Waiting to go out with this chat's other recent notifications",
  sent: "Sent",
  merged: "Sent as part of a later notification from the same chat",
  limited: "Not sent: too many notifications in a short time",
  no_devices: "Not sent: no device had notifications on",
  cancelled: "Not sent: notifications were reset before it went out",
};

function deliveryText(delivery) {
  const answer = delivery.status_code === null ? "no answer" : `answered ${delivery.status_code}`;
  const outcome = delivery.is_accepted
    ? "delivered"
    : delivery.is_forgotten
      ? "gone, removed"
      : delivery.is_stale
        ? "refused the key"
        : "not delivered";
  return `${delivery.label}: ${outcome} (${delivery.push_provider} ${answer})`;
}

function renderSends(sends) {
  sendsList.replaceChildren();
  noSendsNote.hidden = sends.length > 0;
  for (const send of sends) {
    const item = document.createElement("li");
    item.className = "send";
    const heading = document.createElement("span");
    const sender = send.caller === "test" ? "Test from this page" : send.chat_title;
    heading.textContent = `${new Date(send.requested_at).toLocaleString()}: ${sender}`;
    const status = document.createElement("span");
    status.className = "outcome";
    status.textContent = SEND_STATUS_TEXT[send.status] || send.status;
    item.append(heading, status);
    if (send.shown_title !== null) {
      const shown = document.createElement("span");
      shown.className = "outcome";
      shown.textContent = `Showed: ${send.shown_title}: ${send.shown_body}`;
      item.append(shown);
    }
    for (const delivery of send.deliveries) {
      const line = document.createElement("span");
      line.className = "outcome";
      line.textContent = deliveryText(delivery);
      item.append(line);
    }
    const asked = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "What was asked";
    const text = document.createElement("pre");
    text.textContent = send.requested_text;
    asked.append(summary, text);
    item.append(asked);
    sendsList.append(item);
  }
}

async function refreshSends() {
  try {
    renderSends((await requestJson("GET", "api/web-push/sends")).sends);
  } catch (error) {
    noSendsNote.hidden = false;
    noSendsNote.textContent = `Could not read the recent notifications: ${error.message}`;
  }
}

function showResetConfirm(isShown) {
  resetButton.hidden = isShown;
  resetConfirmButton.hidden = !isShown;
  resetCancelButton.hidden = !isShown;
}

async function resetNotifications() {
  resetConfirmButton.disabled = true;
  try {
    const answer = await requestJson("POST", "api/web-push/reset");
    showAnswer(answer);
    const subscription = await currentSubscription();
    if (subscription) await subscription.unsubscribe();
    publicKey = (await requestJson("GET", "api/web-push/key")).public_key;
    await render();
    showStatus("Notifications are off on every device. Turn them on again on each device you want them on.", "neutral");
  } catch (error) {
    showStatus(`Could not reset notifications: ${error.message}`, "bad");
  } finally {
    resetConfirmButton.disabled = false;
    showResetConfirm(false);
  }
}

async function registerSubscription(subscription) {
  const device = await requestJson("POST", "api/web-push/devices", {
    subscription: subscription.toJSON(),
    label: deviceLabel(),
  });
  showAnswer(device);
  await render();
}

async function turnOn() {
  turnOnButton.disabled = true;
  try {
    // Asked first, while the tap still counts as one: Safari grants the prompt only from a user gesture.
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      await render();
      return;
    }
    // A subscription the browser still holds is not one the workspace has (else this button would not show): it may
    // be under an old key, or one its push service already called gone, so it is replaced.
    const existing = await registration.pushManager.getSubscription();
    if (existing) await existing.unsubscribe();
    // A stale entry this device left (its push service refused the key) is replaced by the new subscription.
    const replacedDeviceId = thisDeviceId;
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(publicKey),
    });
    const newDeviceId = await deviceIdOf(subscription);
    if (replacedDeviceId !== null && replacedDeviceId !== newDeviceId) {
      try {
        await requestJson("DELETE", `api/web-push/devices/${encodeURIComponent(replacedDeviceId)}`);
      } catch (error) {
        console.warn("Could not remove this device's earlier entry", error);
      }
    }
    await registerSubscription(subscription);
  } catch (error) {
    showStatus(`Could not turn notifications on: ${error.message}`, "bad");
  } finally {
    turnOnButton.disabled = false;
  }
}

async function sendTest() {
  sendTestButton.disabled = true;
  try {
    const outcome = await requestJson("POST", `api/web-push/devices/${encodeURIComponent(thisDeviceId)}/test`);
    showAnswer(outcome);
    const isForgotten = outcome.deliveries.some((delivery) => delivery.is_forgotten);
    const isStale = outcome.deliveries.some((delivery) => delivery.is_stale);
    // The workspace forgot this device or marked it stale, so the list and the buttons change with it.
    if (isForgotten || isStale) await render();
    else await refreshSends();
    showStatus(
      outcome.accepted_count > 0
        ? "Sent. The notification should appear in a few seconds; if it does not, check this device's " +
            "notification settings and Focus or Do Not Disturb."
        : isForgotten
          ? "The push service says this device's subscription has expired. Turn notifications on again."
          : isStale
            ? "The push service refused this workspace's key. Turn notifications on again."
            : "The push service did not take the notification. The details are below.",
      outcome.accepted_count > 0 ? "good" : "bad",
    );
  } catch (error) {
    showStatus(`Could not send a test: ${error.message}`, "bad");
  } finally {
    sendTestButton.disabled = false;
  }
}

async function forgetDevice(deviceId) {
  try {
    await requestJson("DELETE", `api/web-push/devices/${encodeURIComponent(deviceId)}`);
  } catch (error) {
    await render();
    showStatus(`Could not remove the device: ${error.message}`, "bad");
    return;
  }
  if (deviceId === thisDeviceId) {
    // The workspace has forgotten the device; a subscription the browser keeps is replaced when it is turned on again.
    try {
      const subscription = await currentSubscription();
      if (subscription) await subscription.unsubscribe();
    } catch (error) {
      console.warn("Removed this device, but could not drop this browser's push subscription", error);
    }
  }
  await render();
}

async function turnOff() {
  turnOffButton.disabled = true;
  try {
    await forgetDevice(thisDeviceId);
  } finally {
    turnOffButton.disabled = false;
  }
}

async function start() {
  turnOnButton.addEventListener("click", turnOn);
  sendTestButton.addEventListener("click", sendTest);
  turnOffButton.addEventListener("click", turnOff);
  fullTextBox.addEventListener("change", saveFullText);
  resetButton.addEventListener("click", () => showResetConfirm(true));
  resetCancelButton.addEventListener("click", () => showResetConfirm(false));
  resetConfirmButton.addEventListener("click", resetNotifications);
  if (isSupported) {
    try {
      publicKey = (await requestJson("GET", "api/web-push/key")).public_key;
      await navigator.serviceWorker.register("sw.js", { scope: "./" });
      registration = await navigator.serviceWorker.ready;
    } catch (error) {
      showStatus(`This device cannot get notifications from the workspace: ${error.message}`, "bad");
      return;
    }
  }
  await render();
}

start();
