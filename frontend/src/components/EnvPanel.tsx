import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  getListSecretsApiV1SecretsGetQueryKey,
  useDeleteSecretApiV1SecretsNameDelete,
  useListSecretsApiV1SecretsGet,
  usePutSecretApiV1SecretsNamePut,
} from "@/api/generated/secrets/secrets";
import type { SecretRecordInfo } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { PageHeader } from "@/components/PageHeader";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatUtcTimestamp } from "@/lib/formatTimestamp";
import { cn } from "@/lib/utils";

const TABLE_GRID = "md:grid-cols-[1.2fr_1fr_1.2fr_1.2fr_150px] md:gap-3";

type SecretDialog = { mode: "register" } | { mode: "update"; name: string } | null;

export function EnvPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data, error, isLoading } = useListSecretsApiV1SecretsGet();
  const [dialog, setDialog] = useState<SecretDialog>(null);
  const [deleteTarget, setDeleteTarget] = useState<SecretRecordInfo | null>(null);

  const invalidateSecrets = () => void queryClient.invalidateQueries({ queryKey: getListSecretsApiV1SecretsGetQueryKey() });
  const deleteSecret = useDeleteSecretApiV1SecretsNameDelete({
    mutation: { onSuccess: () => { setDeleteTarget(null); invalidateSecrets(); } },
  });

  const secrets = data?.secrets ?? [];

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={t("nav.secrets")}
        description={t("env.pageDescription")}
        actions={
          <Button onClick={() => setDialog({ mode: "register" })}>
            <Plus className="size-4" aria-hidden />
            {t("env.registerButton")}
          </Button>
        }
      />
      <ApiErrorAlert error={error} />
      {error ? null : isLoading ? (
        <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
      ) : secrets.length === 0 ? (
        <p className="rounded-xl border border-dashed border-border-strong p-5 text-[13px] text-muted-foreground">{t("env.emptyState")}</p>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border text-[13px]">
          <div aria-hidden className={cn(TABLE_GRID, "hidden border-b border-border bg-subtle px-5 py-2.5 text-xs font-medium text-muted-foreground md:grid")}>
            <span>{t("env.columnName")}</span>
            <span>{t("env.columnValue")}</span>
            <span>{t("env.columnUsedBy")}</span>
            <span>{t("env.columnUpdatedAt")}</span>
            <span />
          </div>
          <ul className="flex flex-col">
            {secrets.map((secret, index) => (
              <li key={secret.name} className={cn(TABLE_GRID, "flex flex-col gap-2 px-5 py-3.5 md:grid md:items-center", index > 0 && "border-t border-border")}>
                <code className="min-w-0 truncate font-mono font-medium">{secret.name}</code>
                <span className="font-mono tracking-[2px] text-muted-foreground" aria-label={t("env.columnValue")}>
                  ••••••••
                </span>
                <div className="flex min-w-0 flex-wrap items-center gap-1">
                  {secret.used_by.length === 0 ? (
                    <span className="text-muted-foreground">{t("env.notUsedByAny")}</span>
                  ) : (
                    secret.used_by.map((appName) => (
                      <Badge key={appName} variant="outline">
                        {appName}
                      </Badge>
                    ))
                  )}
                </div>
                <span className="text-muted-foreground">{formatUtcTimestamp(secret.updated_at)}</span>
                <div className="flex gap-1 md:justify-end">
                  <Button variant="outline" size="sm" onClick={() => setDialog({ mode: "update", name: secret.name })}>
                    {t("env.updateValueButton")}
                  </Button>
                  <Button
                    variant="dangerGhost"
                    size="sm"
                    onClick={() => {
                      deleteSecret.reset();
                      setDeleteTarget(secret);
                    }}
                  >
                    {t("env.deleteButton")}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <SecretDialogContent dialog={dialog} onClose={() => setDialog(null)} onSaved={invalidateSecrets} />

      <AlertDialog open={deleteTarget !== null} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("env.deleteDialogTitle", { name: deleteTarget?.name ?? "" })}</AlertDialogTitle>
          </AlertDialogHeader>
          <ApiErrorAlert error={deleteSecret.error} />
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteSecret.isPending}>{t("common.cancel")}</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={deleteSecret.isPending}
              onClick={() => deleteTarget && deleteSecret.mutate({ name: deleteTarget.name })}
            >
              {t("env.deleteDialogConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function SecretDialogContent({
  dialog,
  onClose,
  onSaved,
}: {
  dialog: SecretDialog;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const putSecret = usePutSecretApiV1SecretsNamePut({
    mutation: { onSuccess: () => { onSaved(); onClose(); setName(""); setValue(""); } },
  });

  const effectiveName = dialog?.mode === "update" ? dialog.name : name;

  return (
    <AlertDialog open={dialog !== null} onOpenChange={(open) => !open && onClose()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {dialog?.mode === "update" ? t("env.dialogTitleUpdate", { name: dialog.name }) : t("env.dialogTitleRegister")}
          </AlertDialogTitle>
        </AlertDialogHeader>
        <div className="flex flex-col gap-3">
          {dialog?.mode === "register" ? (
            <div className="flex flex-col gap-1">
              <Label htmlFor="secret-name">{t("env.nameLabel")}</Label>
              <Input id="secret-name" value={name} onChange={(event) => setName(event.target.value.toUpperCase())} />
              <p className="text-xs text-muted-foreground">{t("env.nameHint")}</p>
            </div>
          ) : null}
          <div className="flex flex-col gap-1">
            <Label htmlFor="secret-value">{t("env.valueLabel")}</Label>
            <Input id="secret-value" type="password" value={value} onChange={(event) => setValue(event.target.value)} />
          </div>
          <ApiErrorAlert error={putSecret.error} />
        </div>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={putSecret.isPending}>{t("common.cancel")}</AlertDialogCancel>
          <Button
            disabled={putSecret.isPending || !effectiveName.trim() || !value}
            onClick={() => putSecret.mutate({ name: effectiveName, data: { value } })}
          >
            {t("env.saveButton")}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
