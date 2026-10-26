import { screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { WifiQrSetupNotice } from "@/components/WifiQrSetupNotice";
import i18n from "@/i18n";
import { renderWithProviders } from "@/test/render";

afterEach(async () => { await i18n.changeLanguage("en"); });

it.each([
  ["en", "Setting up Wi-Fi with QR"],
  ["ja", "QR で Wi-Fi を設定中"],
])("shows QR setup guidance in %s", async (language, text) => {
  await i18n.changeLanguage(language);
  const { rerender } = renderWithProviders(<WifiQrSetupNotice setup={{ mode: "active", screen: "scanning" }} />);
  expect(screen.getByText(text)).toBeInTheDocument();
  rerender(<WifiQrSetupNotice setup={{ mode: "idle", screen: null }} />);
  expect(screen.queryByText(text)).not.toBeInTheDocument();
});

it.each(["idle", "backoff", "unavailable"])("does not advertise QR setup in %s", (mode) => {
  const { container } = renderWithProviders(<WifiQrSetupNotice setup={{ mode, screen: null }} />);
  expect(container).toBeEmptyDOMElement();
});
