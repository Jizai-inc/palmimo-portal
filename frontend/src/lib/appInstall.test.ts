import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { installApp, previewApp } from "@/lib/appInstall";
import { server } from "@/test/server";

const JOB_RESPONSE = {
  job: {
    id: "job-1",
    app_name: "app",
    kind: "install",
    state: "running",
    step: "fetch",
    error: null,
    started_at: 1,
    finished_at: null,
    dropped_bindings: [],
    dropped_params: [],
    lock_generated: false,
  },
};

describe("installApp/previewApp", () => {
  it("sends a git source's manifest field in the JSON body when given", async () => {
    let body: unknown;
    server.use(
      http.post("*/api/v1/apps/install", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(JOB_RESPONSE, { status: 202 });
      }),
    );

    await installApp({
      type: "git",
      url: "https://example.com/repo",
      ref: "main",
      ref_kind: "branch",
      manifest: "palmimo.realtime.toml",
    });

    expect(body).toEqual({
      source: { type: "git", url: "https://example.com/repo", ref: "main", ref_kind: "branch", manifest: "palmimo.realtime.toml" },
    });
  });

  it("omits manifest from the JSON body when not given", async () => {
    let body: unknown;
    server.use(
      http.post("*/api/v1/apps/preview", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ name: "app", description: "d", devices: [], env: [] });
      }),
    );

    await previewApp({ type: "git", url: "https://example.com/repo", ref: "main", ref_kind: "branch" });

    expect((body as { source: object }).source).not.toHaveProperty("manifest");
  });

  it("appends manifest to the zip upload's form data when given", async () => {
    let form: FormData | undefined;
    server.use(
      http.post("*/api/v1/apps/install", async ({ request }) => {
        form = await request.formData();
        return HttpResponse.json(JOB_RESPONSE, { status: 202 });
      }),
    );

    await installApp({ type: "zip", file: new File(["z"], "app.zip"), manifest: "palmimo.realtime.toml" });

    expect(form?.get("manifest")).toBe("palmimo.realtime.toml");
    expect(form?.get("file")).not.toBeNull();
  });

  it("omits manifest from the zip upload's form data when not given", async () => {
    let form: FormData | undefined;
    server.use(
      http.post("*/api/v1/apps/install", async ({ request }) => {
        form = await request.formData();
        return HttpResponse.json(JOB_RESPONSE, { status: 202 });
      }),
    );

    await installApp({ type: "zip", file: new File(["z"], "app.zip") });

    expect(form?.get("manifest")).toBeNull();
  });
});
