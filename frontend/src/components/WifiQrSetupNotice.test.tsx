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

it.each([
  ["en", "QR setup is unavailable because the face firmware is too old.", "Repair face firmware"],
  ["ja", "顔のファームが古いため QR での設定は使えません。", "顔のファームを書き直す"],
])("explains old firmware and links to repair in %s", async (language, text, label) => {
  await i18n.changeLanguage(language);
  renderWithProviders(<WifiQrSetupNotice setup={{ mode: "unavailable", screen: null, reason: "firmware_too_old" }} />);
  expect(screen.getByText(text)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: label })).toHaveAttribute("href", "/settings/update#face-firmware");
});
