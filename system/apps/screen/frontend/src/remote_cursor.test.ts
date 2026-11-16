// @vitest-environment jsdom
/** neko's cursor messages read off the data channel, the image sized to the stage, and the stage's pointer as
 *  control comes and goes. */
import { describe, expect, it } from "vitest";

import {
  MAX_CURSOR_PX,
  OP_CURSOR_IMAGE,
  OP_CURSOR_POSITION,
  RemoteCursor,
  cursorBitmap,
  cursorCss,
  listenForCursor,
  parseCursorMessage,
  pngDataUri,
} from "./remote_cursor";
import type { CursorImage, CursorMessage, Rasterizer } from "./remote_cursor";
import type { Size } from "./view_transform";

const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);

/** A message as neko's ``peer.SendCursorImage`` writes it: the opcode, the total length, the fields, the PNG. */
function imageMessage(image: CursorImage): ArrayBuffer {
  const buffer = new ArrayBuffer(11 + image.png.byteLength);
  const view = new DataView(buffer);
  view.setUint8(0, OP_CURSOR_IMAGE);
  view.setUint16(1, 11 + image.png.byteLength);
  view.setUint16(3, image.width);
  view.setUint16(5, image.height);
  view.setUint16(7, image.xhot);
  view.setUint16(9, image.yhot);
  new Uint8Array(buffer, 11).set(image.png);
  return buffer;
}

function positionMessage(x: number, y: number): ArrayBuffer {
  const buffer = new ArrayBuffer(7);
  const view = new DataView(buffer);
  view.setUint8(0, OP_CURSOR_POSITION);
  view.setUint16(1, 7);
  view.setUint16(3, x);
  view.setUint16(5, y);
  return buffer;
}

const ARROW: CursorImage = { width: 32, height: 32, xhot: 4, yhot: 6, png: PNG };

describe("parseCursorMessage", () => {
  it("reads an image message's size, hotspot and PNG", () => {
    const message = parseCursorMessage(imageMessage(ARROW));
    expect(message?.kind).toBe("image");
    if (message?.kind !== "image") return;
    expect(message.image).toMatchObject({ width: 32, height: 32, xhot: 4, yhot: 6 });
    expect(Array.from(message.image.png)).toEqual(Array.from(PNG));
  });

  it("reads a position message", () => {
    expect(parseCursorMessage(positionMessage(640, 480))).toEqual({ kind: "position", x: 640, y: 480 });
  });

  it("answers null for another opcode or a message cut short", () => {
    const pong = new Uint8Array([0x03, 0, 19, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]).buffer;
    expect(parseCursorMessage(pong)).toBeNull();
    expect(parseCursorMessage(imageMessage(ARROW).slice(0, 9))).toBeNull();
    expect(parseCursorMessage(positionMessage(1, 1).slice(0, 5))).toBeNull();
    expect(parseCursorMessage(new ArrayBuffer(0))).toBeNull();
  });
});

describe("listenForCursor", () => {
  it("reads binary messages off the channel and hands over the cursor ones", () => {
    const channel = { binaryType: "blob" as BinaryType, onmessage: null as ((event: MessageEvent) => void) | null };
    const received: CursorMessage[] = [];
    listenForCursor(channel, (message) => received.push(message));
    expect(channel.binaryType).toBe("arraybuffer");
    channel.onmessage?.(new MessageEvent("message", { data: positionMessage(3, 4) }));
    channel.onmessage?.(new MessageEvent("message", { data: "not binary" }));
    channel.onmessage?.(new MessageEvent("message", { data: new Uint8Array([0x03, 0, 3]).buffer }));
    expect(received).toEqual([{ kind: "position", x: 3, y: 4 }]);
  });
});

describe("cursorBitmap", () => {
  it("keeps a 1:1 image as it is", () => {
    expect(cursorBitmap(ARROW, 1)).toEqual({ width: 32, height: 32, xhot: 4, yhot: 6, isScaled: false });
  });

  it("shrinks the image and its hotspot with the drawn video, and never enlarges it", () => {
    expect(cursorBitmap(ARROW, 0.5)).toEqual({ width: 16, height: 16, xhot: 2, yhot: 3, isScaled: true });
    expect(cursorBitmap(ARROW, 2)).toEqual({ width: 32, height: 32, xhot: 4, yhot: 6, isScaled: false });
  });

  it("caps the image where browsers stop honouring one, keeping the hotspot inside", () => {
    const huge = { width: 256, height: 512, xhot: 255, yhot: 511 };
    expect(cursorBitmap(huge, 1)).toEqual({ width: 64, height: MAX_CURSOR_PX, xhot: 63, yhot: 127, isScaled: true });
  });

  it("treats an unsized stage or image as 1:1", () => {
    expect(cursorBitmap(ARROW, 0)).toEqual({ width: 32, height: 32, xhot: 4, yhot: 6, isScaled: false });
    expect(cursorBitmap({ width: 0, height: 0, xhot: 0, yhot: 0 }, 1)).toEqual({
      width: 1,
      height: 1,
      xhot: 0,
      yhot: 0,
      isScaled: false,
    });
  });
});

describe("cursorCss and pngDataUri", () => {
  it("spell the image with its hotspot and the browser's pointer behind it", () => {
    expect(pngDataUri(new Uint8Array([0x89, 0x50, 0x4e, 0x47]))).toBe("data:image/png;base64,iVBORw==");
    expect(cursorCss("data:image/png;base64,iVBORw==", { xhot: 4, yhot: 6 })).toBe(
      'url("data:image/png;base64,iVBORw==") 4 6, auto',
    );
  });
});

describe("RemoteCursor", () => {
  function build(rasterize?: Rasterizer) {
    const stage = document.createElement("div");
    document.body.append(stage);
    const rendered: Size[] = [];
    const cursor = new RemoteCursor(
      stage,
      rasterize ??
        ((_png, size) => {
          rendered.push({ width: size.width, height: size.height });
          return Promise.resolve(`data:image/png;base64,scaled${size.width}x${size.height}`);
        }),
    );
    return { stage, cursor, rendered };
  }

  it("gives the controlling viewer's pointer the remote shape, and the default pointer to a watcher", async () => {
    const { stage, cursor } = build();
    await cursor.setImage(ARROW);
    expect(stage.style.cursor).toBe("");
    cursor.setControlling(true);
    expect(stage.style.cursor).toBe(`url("${pngDataUri(PNG)}") 4 6, auto`);
    cursor.setControlling(false);
    expect(stage.style.cursor).toBe("");
    cursor.setControlling(true);
    expect(stage.style.cursor).toBe(`url("${pngDataUri(PNG)}") 4 6, auto`);
  });

  it("follows a new image while controlling and drops the shape when cleared", async () => {
    const { stage, cursor } = build();
    cursor.setControlling(true);
    await cursor.setImage(ARROW);
    await cursor.setImage({ ...ARROW, xhot: 1, yhot: 1 });
    expect(stage.style.cursor).toBe(`url("${pngDataUri(PNG)}") 1 1, auto`);
    cursor.clear();
    expect(stage.style.cursor).toBe("");
    expect(cursor.currentCss).toBeNull();
  });

  it("resamples the image to the stage's scale and only when its size changes", async () => {
    const { stage, cursor, rendered } = build();
    cursor.setControlling(true);
    await cursor.setScale(0.5);
    await cursor.setImage(ARROW);
    expect(stage.style.cursor).toBe('url("data:image/png;base64,scaled16x16") 2 3, auto');
    await cursor.setScale(0.51);
    await cursor.setScale(0.25);
    expect(stage.style.cursor).toBe('url("data:image/png;base64,scaled8x8") 1 2, auto');
    await cursor.setScale(1);
    expect(stage.style.cursor).toBe(`url("${pngDataUri(PNG)}") 4 6, auto`);
    expect(rendered).toEqual([
      { width: 16, height: 16 },
      { width: 8, height: 8 },
    ]);
  });

  it("falls back to the browser's pointer when the image cannot be drawn or the value is refused", async () => {
    const failing = build(() => Promise.reject(new Error("no canvas")));
    failing.cursor.setControlling(true);
    await failing.cursor.setScale(0.5);
    await failing.cursor.setImage(ARROW);
    expect(failing.stage.style.cursor).toBe("");
    expect(failing.cursor.currentCss).toBeNull();

    const unparsable = build(() => Promise.resolve('x") garbage ("'));
    unparsable.cursor.setControlling(true);
    await unparsable.cursor.setScale(0.5);
    await unparsable.cursor.setImage(ARROW);
    expect(unparsable.stage.style.cursor).toBe("");
  });

  it("ignores a render overtaken by a newer image", async () => {
    let release: (uri: string) => void = () => undefined;
    const slow = new Promise<string>((resolve) => {
      release = resolve;
    });
    const { stage, cursor } = build((_png, size) =>
      size.width === 16 ? slow : Promise.resolve(`data:image/png;base64,fast${size.width}`),
    );
    cursor.setControlling(true);
    await cursor.setScale(0.5);
    const first = cursor.setImage(ARROW);
    await cursor.setImage({ ...ARROW, width: 16, height: 16, xhot: 0, yhot: 0 });
    release("data:image/png;base64,late");
    await first;
    expect(stage.style.cursor).toBe('url("data:image/png;base64,fast8") 0 0, auto');
  });
});
