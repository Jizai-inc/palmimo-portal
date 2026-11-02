import { useQueryClient } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getGetAppApiV1AppsNameGetQueryKey,
  getListAppsApiV1AppsGetQueryKey,
  usePutLabelApiV1AppsNameLabelPut,
} from "@/api/generated/apps/apps";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AppIdLabel, appIdNamePart } from "@/components/AppIdLabel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/** Mirrors the backend's `APP_LABEL_MAX_LENGTH`. */
const LABEL_MAX_LENGTH = 64;

/** The owner-chosen label when set, otherwise the styled id. */
export function AppTitle({ app, className }: { app: { id: string; label?: string | null }; className?: string }) {
  return app.label ? <span className={cn("font-semibold", className)}>{app.label}</span> : <AppIdLabel id={app.id} className={className} />;
}

export function AppTitleEditor({ app }: { app: { id: string; label?: string | null } }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const putLabel = usePutLabelApiV1AppsNameLabelPut({
    mutation: {
      onSuccess: (detail) => {
        queryClient.setQueryData(getGetAppApiV1AppsNameGetQueryKey(app.id), detail);
        void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });
        setEditing(false);
      },
    },
  });

  function startEditing() {
    setDraft(app.label ?? appIdNamePart(app.id));
    putLabel.reset();
    setEditing(true);
  }

  function save() {
    putLabel.mutate({ name: app.id, data: { label: draft } });
  }

  if (editing) {
    return (
      <form
        className="flex w-full flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <div className="flex flex-wrap items-center gap-2">
          <Input
            aria-label={t("appDetail.labelInputLabel")}
            className="max-w-md flex-1"
            value={draft}
            maxLength={LABEL_MAX_LENGTH}
            autoFocus
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setEditing(false);
            }}
          />
          <Button type="submit" disabled={putLabel.isPending}>
            {t("common.save")}
          </Button>
          <Button type="button" variant="ghost" onClick={() => setEditing(false)}>
            {t("common.cancel")}
          </Button>
        </div>
        <ApiErrorAlert error={putLabel.error} />
      </form>
    );
  }

  return (
    <div className="flex min-w-0 flex-col">
      <div className="flex min-w-0 items-center gap-1">
        <h2 className="text-xl [overflow-wrap:anywhere]">
          <AppTitle app={app} />
        </h2>
        <Button type="button" variant="ghost" size="icon" className="size-8 shrink-0" aria-label={t("appDetail.labelEditButton")} onClick={startEditing}>
          <Pencil className="size-4" aria-hidden />
        </Button>
      </div>
      {app.label ? <AppIdLabel id={app.id} className="font-mono text-xs [overflow-wrap:anywhere]" /> : null}
    </div>
  );
}
