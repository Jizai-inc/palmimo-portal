import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { AnimationItem } from "lottie-web";

let sourceQueue: Promise<unknown> = Promise.resolve();

function fetchSource(src: string, signal: AbortSignal): Promise<Response> {
  const request = sourceQueue.catch(() => undefined).then(async () => {
    for (let attempt = 0; ; attempt++) {
      signal.throwIfAborted();
      const response = await fetch(src, { credentials: "include", cache: "no-store", signal });
      if (response.status !== 409 || attempt === 3) return response;
      await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
    }
  });
  sourceQueue = request.catch(() => undefined);
  return request;
}

export function DisplayAssetPreview({ src, label }: { src: string; label: string }) {
  return <Preview key={src} src={src} label={label} />;
}

function Preview({ src, label }: { src: string; label: string }) {
  const { t } = useTranslation();
  const container = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<"loading" | "image" | "lottie" | "failed">("loading");
  const [imageUrl, setImageUrl] = useState<string>();

  useEffect(() => {
    const controller = new AbortController();
    let animation: AnimationItem | undefined;
    let objectUrl: string | undefined;
    const fail = () => {
      if (controller.signal.aborted) return;
      animation?.destroy();
      animation = undefined;
      setState("failed");
    };
    const load = async () => {
      const response = await fetchSource(src, controller.signal);
      if (!response.ok) throw new Error("source unavailable");
      const mediaType = response.headers.get("content-type")?.split(";")[0];
      if (mediaType === "application/json") {
        const data = await response.json();
        if (!data || !Array.isArray(data.layers)) throw new Error("invalid animation");
        // Uploaded animations must not make the browser fetch external assets.
        if (data.assets?.some((asset: { layers?: unknown; p?: string; e?: number; u?: string }) => !asset.layers &&
          (typeof asset.p !== "string" || asset.e !== 1 || asset.u || !/^data:image\/(png|jpeg|gif);base64,/.test(asset.p))) ||
          data.fonts?.list?.some((font: { fPath?: string }) => font.fPath)) {
          throw new Error("external animation asset");
        }
        const { default: lottie } = await import("lottie-web/build/player/lottie_light");
        if (controller.signal.aborted || !container.current) return;
        const config = { container: container.current, renderer: "svg" as const, loop: true, autoplay: true, autoloadSegments: false, animationData: data };
        animation = lottie.loadAnimation(config);
        animation.addEventListener("data_failed", fail);
        animation.addEventListener("error", fail);
        setState("lottie");
      } else if (["image/png", "image/jpeg", "image/gif"].includes(mediaType ?? "")) {
        const blob = await response.blob();
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setImageUrl(objectUrl);
        setState("image");
      } else throw new Error("unsupported source");
    };
    void load().catch(fail);
    return () => {
      controller.abort();
      animation?.destroy();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src]);

  return <div className="flex shrink-0 flex-col items-center gap-1">
    <div className="flex size-20 items-center justify-center overflow-hidden rounded-lg bg-black">
      {state === "image" && <img src={imageUrl} alt={label} className="size-full object-contain" onError={() => setState("failed")} />}
      <div ref={container} role={state === "lottie" ? "img" : undefined} aria-label={state === "lottie" ? label : undefined} className={state === "lottie" ? "size-full" : "hidden"} />
      {state === "failed" && <span role="status" className="px-1 text-center text-[10px] text-white">{t("displayPreview.unavailable")}</span>}
    </div>
  </div>;
}

export function DisplayPreviewNote() {
  const { t } = useTranslation();
  return <p className="text-xs text-muted-foreground">{t("displayPreview.appearanceNote")}</p>;
}
