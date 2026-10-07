"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// FR03: capture a note with the device camera. Retake discards only the temporary capture; closing
// always stops the camera tracks. If the camera is blocked or missing, the file picker is offered.

const BLOCKED = "Camera access is blocked. Allow access or choose a file.";

type Props = { open: boolean; onClose: () => void; onUse: (file: File) => void };

export function CameraCapture({ open, onClose, onUse }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const fallbackRef = useRef<HTMLInputElement>(null);
  const [shot, setShot] = useState<{ blob: Blob; url: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const stop = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, []);

  /** Resolves to null when the camera is live, or to the message explaining why it is not. */
  const start = useCallback(async (): Promise<string | null> => {
    if (!navigator.mediaDevices?.getUserMedia) {
      return "This browser cannot use the camera here. Choose a file instead.";
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 2560 }, height: { ideal: 1920 } },
        audio: false,
      });
      streamRef.current = stream;
      if (videoRef.current) videoRef.current.srcObject = stream;
      return null;
    } catch (e) {
      const name = (e as DOMException)?.name;
      return name === "NotFoundError" ? "No camera was found. Choose a file instead." : BLOCKED;
    }
  }, []);

  const discardShot = useCallback(() => {
    setShot((s) => {
      if (s) URL.revokeObjectURL(s.url);
      return null;
    });
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open) {
      if (!dialog.open) dialog.showModal(); // native modal: focus is contained and Escape closes it
      void start().then(setError);
    } else {
      if (dialog.open) dialog.close();
      stop();
    }
  }, [open, start, stop]);

  // After a retake the <video> remounts; give it the live stream again.
  useEffect(() => {
    if (!shot && videoRef.current && streamRef.current) videoRef.current.srcObject = streamRef.current;
  }, [shot]);

  useEffect(() => stop, [stop]); // unmount: always release the camera

  function close() {
    stop();
    discardShot();
    setError(null);
    onClose();
  }

  function capture() {
    const video = videoRef.current;
    if (!video || !video.videoWidth) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0);
    canvas.toBlob((blob) => blob && setShot({ blob, url: URL.createObjectURL(blob) }), "image/jpeg", 0.92);
  }

  function use() {
    if (!shot) return;
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    onUse(new File([shot.blob], `camera-${stamp}.jpg`, { type: "image/jpeg" }));
    close();
  }

  return (
    <dialog ref={dialogRef} aria-labelledby="camera-title" onClose={close} onCancel={close}>
      <div className="stack">
        <h2 id="camera-title">Capture a production note</h2>
        {error ? (
          <div className="stack">
            <div className="banner warning" role="alert">
              {error}
            </div>
            <input
              ref={fallbackRef}
              type="file"
              accept="image/jpeg,image/png"
              capture="environment"
              className="sr-only"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) {
                  onUse(f);
                  close();
                }
              }}
            />
            <button className="primary" onClick={() => fallbackRef.current?.click()}>
              Choose a file
            </button>
          </div>
        ) : shot ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img className="capture-preview" src={shot.url} alt="Captured note preview" />
        ) : (
          <video ref={videoRef} autoPlay playsInline muted aria-label="Camera preview" />
        )}
        <div className="row">
          {!error && !shot && (
            <button className="primary" onClick={capture}>
              Capture photo
            </button>
          )}
          {shot && (
            <>
              <button className="primary" onClick={use}>
                Use photo
              </button>
              <button onClick={discardShot}>Retake</button>
            </>
          )}
          <button onClick={close}>Close camera</button>
        </div>
        <p className="meta">Hold the note flat, fill the frame and avoid shadows for the best reading.</p>
      </div>
    </dialog>
  );
}
