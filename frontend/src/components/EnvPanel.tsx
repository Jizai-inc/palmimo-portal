import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getListGitCredentialsApiV1GitCredentialsGetQueryKey,
  getListSecretsApiV1SecretsGetQueryKey,
  useDeleteGitCredentialApiV1GitCredentialsHostOwnerDelete,
  useDeleteSecretApiV1SecretsNameDelete,
  useListGitCredentialsApiV1GitCredentialsGet,
  useListSecretsApiV1SecretsGet,
  usePutGitCredentialApiV1GitCredentialsHostOwnerPut,
  usePutSecretApiV1SecretsNamePut,
} from "@/api/generated/secrets/secrets";
import type { SecretRecordInfo } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatUtcTimestamp } from "@/lib/formatTimestamp";

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
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-2">
          <p className="font-semibold">{t("env.title")}</p>
          <Button onClick={() => setDialog({ mode: "register" })}>{t("env.registerButton")}</Button>
        </div>
        <ApiErrorAlert error={error} />
        {error ? null : isLoading ? (
          <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
        ) : secrets.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("env.emptyState")}</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {secrets.map((secret) => (
              <li key={secret.name} className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4 md:flex-row md:items-center md:gap-4">
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <span className="sr-only">{t("env.columnName")}</span>
                  <code className="font-mono text-sm">{secret.name}</code>
                  <span className="sr-only">{t("env.columnValue")}</span>
                  <span className="font-mono text-xs text-muted-foreground">••••••••</span>
                  <span className="text-xs text-muted-foreground">{t("env.columnUpdatedAt")}: {formatUtcTimestamp(secret.updated_at)}</span>
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="text-xs text-muted-foreground">{t("env.columnUsedBy")}:</span>
                    {secret.used_by.length === 0 ? (
                      <span className="text-xs text-muted-foreground">{t("env.notUsedByAny")}</span>
                    ) : (
                      secret.used_by.map((appName) => (
                        <Badge key={appName} variant="outline">{appName}</Badge>
                      ))
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 gap-2">
                  <Button variant="outline" onClick={() => setDialog({ mode: "update", name: secret.name })}>
                    {t("env.updateValueButton")}
                  </Button>
                  <Button variant="outline" onClick={() => { deleteSecret.reset(); setDeleteTarget(secret); }}>
                    {t("env.deleteButton")}
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <GitCredentialsSection />

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

function GitCredentialsSection() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [hostOwner, setHostOwner] = useState("");
  const [token, setToken] = useState("");
  const [saved, setSaved] = useState(false);

  const { data } = useListGitCredentialsApiV1GitCredentialsGet();
  const credentials = data?.credentials ?? [];
  const invalidateCredentials = () =>
    void queryClient.invalidateQueries({ queryKey: getListGitCredentialsApiV1GitCredentialsGetQueryKey() });

  const putCredential = usePutGitCredentialApiV1GitCredentialsHostOwnerPut({
    mutation: {
      onSuccess: () => {
        setAddOpen(false);
        setHostOwner("");
        setToken("");
        setSaved(true);
        invalidateCredentials();
        setTimeout(() => setSaved(false), 2000);
      },
    },
  });
  const deleteCredential = useDeleteGitCredentialApiV1GitCredentialsHostOwnerDelete({
    mutation: { onSuccess: invalidateCredentials },
  });

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-2">
        <p className="font-semibold">{t("env.gitCredentialsTitle")}</p>
        <Button variant="outline" onClick={() => setAddOpen(true)}>{t("env.gitCredentialsAddButton")}</Button>
      </div>
      {saved ? <p className="text-sm text-primary">{t("env.gitCredentialsSaved")}</p> : null}

      {credentials.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("env.gitCredentialsEmptyState")}</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {credentials.map((credential) => (
            <li
              key={credential.host_owner}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-2"
            >
              <div className="flex flex-wrap items-center gap-2">
                <code className="font-mono text-sm">{credential.host_owner}</code>
                <span className="text-xs text-muted-foreground">
                  {t("env.columnUpdatedAt")}: {formatUtcTimestamp(credential.updated_at)}
                </span>
                {credential.rejected_at != null ? (
                  <Badge variant="destructive">{t("env.gitCredentialsRejectedBadge")}</Badge>
                ) : null}
              </div>
              <Button
                variant="outline"
                disabled={deleteCredential.isPending}
                onClick={() => deleteCredential.mutate({ hostOwner: credential.host_owner })}
              >
                {t("env.deleteButton")}
              </Button>
            </li>
          ))}
        </ul>
      )}
      <ApiErrorAlert error={deleteCredential.error} />

      <AlertDialog open={addOpen} onOpenChange={setAddOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("env.gitCredentialsDialogTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("env.gitCredentialsHostOwnerHint")}</AlertDialogDescription>
          </AlertDialogHeader>
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <Label htmlFor="git-host-owner">{t("env.gitCredentialsHostOwnerLabel")}</Label>
              <Input id="git-host-owner" value={hostOwner} onChange={(event) => setHostOwner(event.target.value)} placeholder="github.com/Jizai-inc" />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="git-token">{t("env.gitCredentialsTokenLabel")}</Label>
              <Input id="git-token" type="password" value={token} onChange={(event) => setToken(event.target.value)} />
            </div>
            <ApiErrorAlert error={putCredential.error} />
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={putCredential.isPending}>{t("common.cancel")}</AlertDialogCancel>
            <Button
              disabled={putCredential.isPending || !hostOwner.trim() || !token}
              onClick={() => putCredential.mutate({ hostOwner, data: { value: token } })}
            >
              {t("env.saveButton")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
