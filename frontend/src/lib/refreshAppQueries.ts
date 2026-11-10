import type { QueryClient } from "@tanstack/react-query";
import { getGetAppApiV1AppsNameGetQueryKey, getListAppsApiV1AppsGetQueryKey } from "@/api/generated/apps/apps";
import { getGetPublicCatalogApiV1CatalogPublicGetQueryKey } from "@/api/generated/catalog/catalog";

export function refreshAppQueries(client: QueryClient, appId?: string) {
  return Promise.all([
    client.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey(), refetchType: "all" }),
    client.invalidateQueries({ queryKey: getGetPublicCatalogApiV1CatalogPublicGetQueryKey(), refetchType: "all" }),
    ...(appId ? [client.invalidateQueries({ queryKey: getGetAppApiV1AppsNameGetQueryKey(appId) })] : []),
  ]);
}
