export {};

declare global {
  interface Window {
    obMonaco?: { getWorkerUrl: (type: string) => string };
  }
  interface Document {
    webkitFullscreenElement?: Element | null;
    mozFullScreenElement?: Element | null;
    msFullscreenElement?: Element | null;
    webkitExitFullscreen?: () => void;
    msExitFullscreen?: () => void;
    mozCancelFullScreen?: () => void;
  }

  interface HTMLIFrameElement {
    webkitRequestFullscreen?: () => void;
    msRequestFullscreen?: () => void;
    mozRequestFullScreen?: () => void;
  }
}
