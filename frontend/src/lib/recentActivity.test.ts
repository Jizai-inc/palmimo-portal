import { describe, expect, it } from "vitest";

import { recentActivity } from "@/lib/recentActivity";
import { appSummary, finishedJob } from "@/test/appSummaryFixture";

describe("recentActivity", () => {
  it("merges app jobs and the last Wi-Fi attempt newest first", () => {
    const apps = [
      appSummary({ id: "a.old", name: "old", last_job: finishedJob("install", "done", 100) }),
      appSummary({ id: "a.new", name: "new", last_job: finishedJob("update", "failed", 300) }),
    ];

    const items = recentActivity({ apps, lastWifiAttempt: { ssid: "Home", result: "connected", timestamp: 200 } });

    expect(items.map((item) => item.at)).toEqual([300, 200, 100]);
    expect(items[0]).toMatchObject({ kind: "appJob", job: "update", ok: false, appName: "new" });
    expect(items[1]).toMatchObject({ kind: "wifi", ssid: "Home", ok: true });
  });

  it("skips running jobs and an in-progress Wi-Fi attempt", () => {
    const running = { ...finishedJob("install", "done", 100), state: "running", finished_at: null };

    const items = recentActivity({
      apps: [appSummary({ last_job: running })],
      lastWifiAttempt: { ssid: "Home", result: "attempting", timestamp: 50 },
    });

    expect(items).toEqual([]);
  });

  it("keeps at most five items", () => {
    const apps = Array.from({ length: 7 }, (_, index) =>
      appSummary({ id: `a.${index}`, last_job: finishedJob("install", "done", index + 1) }),
    );

    expect(recentActivity({ apps, lastWifiAttempt: null })).toHaveLength(5);
  });
});
