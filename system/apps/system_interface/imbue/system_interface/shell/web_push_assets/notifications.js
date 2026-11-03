// The notifications page (README, "Notifications on a phone"): turns Web Push on or off for the device it is opened
// on, sends that device a test, and lists every device that has it on. Every URL is relative to the page, so the page
// works wherever the shell's root is.

const statusElement = document.getElementById("status");
const turnOnButton = document.getElementById("turn-on");
const sendTestButton = document.getElementById("send-test");
const turnOffButton = document.getElementById("turn-off");
const devicesList = document.getElementById("devices");
const noDevicesNote = document.getElementById("no-devices");
const noDevicesText = noDevicesNote.textContent;
const lastAnswer = document.getElementById("last-answer");
const lastAnswerBody = document.getElementById("last-answer-body");

const isSupported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const isAppleMobile =
  /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isStandalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

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
    detail.textContent = `${device.push_provider}, since ${new Date(device.registered_at).toLocaleString()}`;
    description.append(detail);
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
  const isRegistered = thisDeviceId !== null && devices.some((device) => device.device_id === thisDeviceId);
  showButtons({ isOn: isRegistered });
  showStatus(
    isRegistered ? "Notifications are on for this device." : "Notifications are off for this device.",
    isRegistered ? "good" : "neutral",
  );
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
    const subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(publicKey),
    });
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
    // The workspace forgot this device, so the list and the buttons change with it.
    if (isForgotten) await render();
    showStatus(
      outcome.accepted_count > 0
        ? "Sent. The notification should appear in a few seconds; if it does not, check this device's " +
            "notification settings and Focus or Do Not Disturb."
        : isForgotten
          ? "The push service says this device's subscription has expired. Turn notifications on again."
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
