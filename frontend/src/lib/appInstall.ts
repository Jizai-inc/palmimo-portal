// `POST /apps/install` and `POST /apps/preview` accept either a multipart zip upload or a JSON
// git-source body (design doc 3.2); FastAPI reads the raw `Request` there instead of a single
// Pydantic model, so orval generated neither endpoint with a typed request body (see
// `src/api/generated/apps/apps.ts`'s `installApiV1AppsInstallPost`/`previewApiV1AppsPreviewPost`,
// both parameterless). These wrappers fill that gap by hand, reusing the generated URL getters
// and `customFetch` so error handling (`PortalApiError`) still matches every other call.
import {
  getInstallApiV1AppsInstallPostUrl,
  getPreviewApiV1AppsPreviewPostUrl,
} from "@/api/generated/apps/apps";
import type { AppJobAcceptedResponse, ManifestPreviewResponse } from "@/api/generated/models";
import { customFetch } from "@/api/client";

export interface GitInstallSource {
  type: "git";
  url: string;
  ref: string;
  ref_kind: "branch" | "tag";
  subdir?: string;
  manifest?: string;
}

export type InstallSource = GitInstallSource | { type: "zip"; file: File; manifest?: string };

function toRequestInit(source: InstallSource): RequestInit {
  if (source.type === "zip") {
    const formData = new FormData();
    formData.append("file", source.file);
    if (source.manifest) formData.append("manifest", source.manifest);
    return { method: "POST", body: formData };
  }
  const { type, url, ref, ref_kind, subdir, manifest } = source;
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type, url, ref, ref_kind, ...(subdir ? { subdir } : {}), ...(manifest ? { manifest } : {}) }),
  };
}

export function installApp(source: InstallSource): Promise<AppJobAcceptedResponse> {
  return customFetch<AppJobAcceptedResponse>(getInstallApiV1AppsInstallPostUrl(), toRequestInit(source));
}

export function previewApp(source: InstallSource): Promise<ManifestPreviewResponse> {
  return customFetch<ManifestPreviewResponse>(getPreviewApiV1AppsPreviewPostUrl(), toRequestInit(source));
}
