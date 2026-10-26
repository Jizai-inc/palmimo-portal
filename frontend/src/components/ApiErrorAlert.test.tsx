import { screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { expect, it } from "vitest";

import { PortalApiError } from "@/api/client";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

it.each(["app_job_in_progress", "app_busy"])("explains %s during platform maintenance", async (code) => {
  server.use(http.get("*/api/v1/platform/update", () => HttpResponse.json({
    job: { state: "running", step: "display_firmware", target_version: 2, error: null, started_at: 1, finished_at: null },
  })));
  renderWithProviders(<ApiErrorAlert error={new PortalApiError(409, code, {})} />);
  expect(await screen.findByText("The device platform is updating. Try again when it finishes.")).toBeInTheDocument();
});
