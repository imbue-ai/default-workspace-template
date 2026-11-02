/**
 * The remote cursor as the controller's own pointer (docs/system/specs/screen/spec.md section 12). No pointer is
 * in the video (``show_pointer`` is off); neko sends the X cursor over the peer's data channel instead
 * (``server/internal/webrtc/payload``: a header of the opcode and a big-endian ``uint16`` length, then for
 * ``OP_CURSOR_IMAGE`` width, height and hotspot as ``uint16`` and the PNG, for ``OP_CURSOR_POSITION`` x and y),
 * the image to every peer when its channel opens and on every change, the position to peers that are not the
 * host. The controlling viewer gives the stage's CSS cursor that image and hotspot, shrunk with the drawn video
 * when the stage renders the desktop smaller than 1:1 and capped where browsers stop honouring a cursor image;
 * every other viewer keeps the default pointer, and neither the position nor neko's inactive cursors are drawn.
 */

import type { Size } from "./view_transform";

export const OP_CURSOR_POSITION = 0x01;
export const OP_CURSOR_IMAGE = 0x02;
/** Chrome and Firefox ignore a cursor image larger than this on either side. */
export const MAX_CURSOR_PX = 128;
const HEADER_BYTES = 3;
const POSITION_BYTES = HEADER_BYTES + 4;
const IMAGE_HEADER_BYTES = HEADER_BYTES + 8;

export interface CursorImage {
  width: number;
  height: number;
  xhot: number;
  yhot: number;
  png: Uint8Array;
}

/** The image as the stage shows it: its size in CSS pixels, the hotspot inside it, and whether it was resampled. */
export interface CursorBitmap extends Size {
  xhot: number;
  yhot: number;
  isScaled: boolean;
}

export type CursorMessage = { kind: "image"; image: CursorImage } | { kind: "position"; x: number; y: number };

/** A message of neko's data channel, or null for one that is not about the cursor or is cut short. */
export function parseCursorMessage(data: ArrayBuffer): CursorMessage | null {
  if (data.byteLength < HEADER_BYTES) return null;
  const view = new DataView(data);
  const opcode = view.getUint8(0);
  if (opcode === OP_CURSOR_POSITION) {
    if (data.byteLength < POSITION_BYTES) return null;
    return { kind: "position", x: view.getUint16(3), y: view.getUint16(5) };
  }
  if (opcode === OP_CURSOR_IMAGE) {
    if (data.byteLength < IMAGE_HEADER_BYTES) return null;
    return {
      kind: "image",
      image: {
        width: view.getUint16(3),
        height: view.getUint16(5),
        xhot: view.getUint16(7),
        yhot: view.getUint16(9),
        png: new Uint8Array(data, IMAGE_HEADER_BYTES),
      },
    };
  }
  return null;
}

/** The channel neko opened on the peer, read for cursor messages. */
export function listenForCursor(
  channel: Pick<RTCDataChannel, "binaryType" | "onmessage">,
  onMessage: (message: CursorMessage) => void,
): void {
  channel.binaryType = "arraybuffer";
  channel.onmessage = (event: MessageEvent) => {
    if (!(event.data instanceof ArrayBuffer)) return;
    const message = parseCursorMessage(event.data);
    if (message !== null) onMessage(message);
  };
}

/** The image at the stage's scale (CSS pixels per Screen pixel): shrunk with the drawn video, never enlarged, and
 *  within the cap on either side; the hotspot moves with it and stays inside. */
export function cursorBitmap(image: Omit<CursorImage, "png">, scale: number): CursorBitmap {
  const fit = Math.min(1, scale, MAX_CURSOR_PX / image.width, MAX_CURSOR_PX / image.height);
  const factor = fit > 0 ? fit : 1;
  const width = Math.max(1, Math.round(image.width * factor));
  const height = Math.max(1, Math.round(image.height * factor));
  return {
    width,
    height,
    xhot: Math.min(width - 1, Math.max(0, Math.round(image.xhot * factor))),
    yhot: Math.min(height - 1, Math.max(0, Math.round(image.yhot * factor))),
    isScaled: factor !== 1,
  };
}

/** The ``cursor`` value: the image with its hotspot, and the browser's own pointer when it will not show the image. */
export function cursorCss(uri: string, hotspot: { xhot: number; yhot: number }): string {
  return `url("${uri}") ${hotspot.xhot} ${hotspot.yhot}, auto`;
}

export function pngDataUri(png: Uint8Array): string {
  let binary = "";
  for (const byte of png) binary += String.fromCharCode(byte);
  return `data:image/png;base64,${btoa(binary)}`;
}

/** The PNG drawn at a size, as a data URI. */
export type Rasterizer = (png: Uint8Array, size: Size) => Promise<string>;

export function rasterizeWithCanvas(png: Uint8Array, size: Size): Promise<string> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = size.width;
      canvas.height = size.height;
      const context = canvas.getContext("2d");
      if (context === null) {
        reject(new Error("no 2d context to draw the cursor with"));
        return;
      }
      context.imageSmoothingQuality = "high";
      context.drawImage(image, 0, 0, size.width, size.height);
      resolve(canvas.toDataURL("image/png"));
    };
    image.onerror = () => reject(new Error("the cursor image did not decode"));
    image.src = pngDataUri(png);
  });
}

export class RemoteCursor {
  private image: CursorImage | null = null;
  private scale = 1;
  private isControlling = false;
  private css: string | null = null;
  private renderSerial = 0;

  constructor(
    private readonly stage: HTMLElement,
    private readonly rasterize: Rasterizer = rasterizeWithCanvas,
  ) {}

  /** The stage's cursor while this viewer controls, or null for the browser's own pointer. */
  get currentCss(): string | null {
    return this.css;
  }

  /** neko's latest cursor; resolves once the stage shows it. */
  setImage(image: CursorImage): Promise<void> {
    this.image = image;
    return this.render();
  }

  /** The shape goes (the peer is gone, or the screencast paints the pointer into its frames). */
  clear(): void {
    this.image = null;
    this.renderSerial++;
    this.css = null;
    this.apply();
  }

  /** How many CSS pixels the stage draws one Screen pixel as; a change that leaves the image's size alone is free. */
  setScale(scale: number): Promise<void> {
    const before = this.image === null ? null : cursorBitmap(this.image, this.scale);
    this.scale = scale;
    if (this.image === null || before === null) return Promise.resolve();
    const after = cursorBitmap(this.image, scale);
    if (before.width === after.width && before.height === after.height) return Promise.resolve();
    return this.render();
  }

  setControlling(isControlling: boolean): void {
    this.isControlling = isControlling;
    this.apply();
  }

  private async render(): Promise<void> {
    const image = this.image;
    if (image === null) return;
    const serial = ++this.renderSerial;
    const bitmap = cursorBitmap(image, this.scale);
    let uri: string | null;
    try {
      uri = bitmap.isScaled ? await this.rasterize(image.png, bitmap) : pngDataUri(image.png);
    } catch (error) {
      console.warn("[screen] the remote cursor could not be drawn", error);
      uri = null;
    }
    // A newer image or scale has been rendered since: this one is stale.
    if (serial !== this.renderSerial) return;
    this.css = uri === null ? null : cursorCss(uri, bitmap);
    this.apply();
  }

  private apply(): void {
    const css = this.isControlling ? this.css : null;
    // Cleared first: a value the browser's parser refuses leaves the property as it was, which is then the
    // stylesheet's pointer rather than a previous image.
    this.stage.style.cursor = "";
    if (css !== null) this.stage.style.cursor = css;
  }
}
