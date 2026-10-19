/** The picture without WebRTC (docs/system/specs/screen/spec.md section 5.4): neko's JPEG screencast, polled
 *  through the service's proxy with the session token, drawn into an image over the stage. Input keeps flowing over
 *  the WebSocket meanwhile. */

export const SCREENCAST_PATH = "/neko/api/room/screen/cast.jpg";
// neko encodes the screencast at this rate (capture.screencast.rate in neko_config.py), so polling faster only
// repeats a frame.
export const SCREENCAST_FPS = 5;
const POLL_INTERVAL_MS = 1000 / SCREENCAST_FPS;
const RETRY_INTERVAL_MS = 1000;

export class ScreencastPoller {
  private timer = 0;
  private isRunning = false;
  private objectUrl: string | null = null;

  constructor(
    private readonly image: HTMLImageElement,
    private readonly token: string,
  ) {}

  start(): void {
    if (this.isRunning) return;
    this.isRunning = true;
    this.image.hidden = false;
    void this.tick();
  }

  stop(): void {
    this.isRunning = false;
    window.clearTimeout(this.timer);
    this.timer = 0;
    this.image.hidden = true;
    this.releaseFrame();
  }

  private releaseFrame(): void {
    if (this.objectUrl !== null) URL.revokeObjectURL(this.objectUrl);
    this.objectUrl = null;
  }

  private async tick(): Promise<void> {
    if (!this.isRunning) return;
    let delay = POLL_INTERVAL_MS;
    try {
      const response = await fetch(SCREENCAST_PATH, {
        headers: { Authorization: `Bearer ${this.token}` },
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`screencast answered ${response.status}`);
      const url = URL.createObjectURL(await response.blob());
      if (!this.isRunning) {
        URL.revokeObjectURL(url);
        return;
      }
      this.releaseFrame();
      this.objectUrl = url;
      this.image.src = url;
    } catch (error) {
      console.warn("[screen] screencast frame failed", error);
      delay = RETRY_INTERVAL_MS;
    }
    if (this.isRunning) this.timer = window.setTimeout(() => void this.tick(), delay);
  }
}
