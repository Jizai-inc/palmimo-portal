import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { AnimationItem } from "lottie-web";

const sourcePools = {
  parallel: { active: 0, waiters: [] as Array<() => void> },
  serialized: { active: 0, waiters: [] as Array<() => void> },
};

async function fetchSource(src: string, signal: AbortSignal, retryBusy: boolean): Promise<Response> {
  const pool = sourcePools[retryBusy ? "serialized" : "parallel"];
  if (pool.active >= (retryBusy ? 1 : 4)) await new Promise<void>((resolve) => pool.waiters.push(resolve));
  else pool.active++;
  try {
    for (let attempt = 0; ; attempt++) {
      signal.throwIfAborted();
      const response = await fetch(src, { credentials: "include", cache: "no-store", signal });
      if (!retryBusy || response.status !== 409 || attempt === 3) return response;
      await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
    }
  } finally {
    const next = pool.waiters.shift();
    if (next) next();
    else pool.active--;
  }
}

type PreviewProps = { src: string; label: string; retryBusy?: boolean };

type LottieAsset = Record<string, unknown> & { sid?: string };

function isSelfContainedAsset(value: unknown): value is LottieAsset {
  if (!value || typeof value !== "object") return false;
  const asset = value as Record<string, unknown>;
  if (asset.sid !== undefined && typeof asset.sid !== "string") return false;
  if (!("p" in asset || "u" in asset || "e" in asset)) return Array.isArray(asset.layers);
  return typeof asset.p === "string" && asset.e === 1 &&
    (asset.u === undefined || asset.u === "") &&
    /^data:image\/(png|jpeg|gif);base64,[A-Za-z0-9+/]+={0,2}$/.test(asset.p);
}

export function DisplayAssetPreview({ src, label, retryBusy = true }: PreviewProps) {
  return <Preview key={src} src={src} label={label} retryBusy={retryBusy} />;
}

function Preview({ src, label, retryBusy = true }: PreviewProps) {
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
      const response = await fetchSource(src, controller.signal, retryBusy);
      if (!response.ok) throw new Error("source unavailable");
      const mediaType = response.headers.get("content-type")?.split(";")[0];
      if (mediaType === "application/json") {
        const data = await response.json();
        if (!data || !Array.isArray(data.layers)) throw new Error("invalid animation");
        // Uploaded animations must not make the browser fetch external assets.
        if (data.assets?.some((asset: unknown) => {
          if (!isSelfContainedAsset(asset)) return true;
          const replacement: unknown = asset.sid ? data.slots?.[asset.sid]?.p : undefined;
          if (replacement === undefined) return false;
          // SlotManager's Object.assign can affect later references to the same asset.
          return !replacement || typeof replacement !== "object" || Array.isArray(replacement) ||
            Object.hasOwn(replacement, "__proto__") || Object.hasOwn(replacement, "sid") ||
            !isSelfContainedAsset({ ...asset, ...replacement });
        }) || data.fonts?.list?.some((font: { fPath?: unknown }) => font.fPath !== undefined && font.fPath !== "")) {
          throw new Error("external animation asset");
        }
        // The device's compact Lottie sources omit the zero layer start time.
        for (const composition of [data, ...(data.assets ?? [])]) {
          for (const layer of composition.layers ?? []) layer.st ??= 0;
        }
        // Compact scalar keyframes need the array form expected by lottie-web.
        const pending = [data];
        while (pending.length) {
          const value = pending.pop();
          if (Array.isArray(value.k)) {
            for (const keyframe of value.k) {
              if (typeof keyframe?.t !== "number") continue;
              if (typeof keyframe.s === "number") keyframe.s = [keyframe.s];
              if (typeof keyframe.e === "number") keyframe.e = [keyframe.e];
            }
          }
          for (const child of Object.values(value)) {
            if (child && typeof child === "object") pending.push(child);
          }
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
  }, [src, retryBusy]);

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
