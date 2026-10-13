import type { Shot } from "./core";

export type ViewportBridge = {
  capture: null | ((shot?: Shot) => string);
  renderFrame: null | ((shot: Shot, time: number) => void);
  getCanvas: null | (() => HTMLCanvasElement | null);
  useEditorAsCamera: null | (() => void);
  prepareExport: null | ((width: number, height: number) => () => void);
};

export const viewportBridge: ViewportBridge = {
  capture: null,
  renderFrame: null,
  getCanvas: null,
  useEditorAsCamera: null,
  prepareExport: null,
};

