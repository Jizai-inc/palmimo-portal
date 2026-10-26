import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  getListGitCredentialsApiV1GitCredentialsGetQueryKey,
  useDeleteGitCredentialApiV1GitCredentialsHostOwnerDelete,
  useListGitCredentialsApiV1GitCredentialsGet,
  usePutGitCredentialApiV1GitCredentialsHostOwnerPut,
} from "@/api/generated/secrets/secrets";
import type { GitCredentialRecordInfo } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { SectionHeader } from "@/components/PageHeader";
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatUtcTimestamp } from "@/lib/formatTimestamp";

type Dialog = { hostOwner?: string } | null;

function normalizedHostOwner(value: string) {
  const [host, ...owner] = value.trim().replace(/\/+$/, "").split("/");
  return `${host.toLowerCase()}/${host.toLowerCase() === "github.com" ? owner.join("/").toLowerCase() : owner.join("/")}`;
}

export function GitCredentialsPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data, error, isLoading } = useListGitCredentialsApiV1GitCredentialsGet();
  const [dialog, setDialog] = useState<Dialog>(null);
  const [deleteTarget, setDeleteTarget] = useState<GitCredentialRecordInfo | null>(null);
  const credentials = data?.credentials ?? [];
  const invalidate = () => void queryClient.invalidateQueries({ queryKey: getListGitCredentialsApiV1GitCredentialsGetQueryKey() });
  const deletion = useDeleteGitCredentialApiV1GitCredentialsHostOwnerDelete({ mutation: { onSuccess: () => { setDeleteTarget(null); invalidate(); } } });
  return <section className="flex flex-col gap-3.5">
    <SectionHeader
      title={t("gitCredentials.title")}
      description={t("gitCredentials.description")}
      actions={<Button variant="outline" onClick={() => setDialog({})}><Plus className="size-4" aria-hidden />{t("gitCredentials.addButton")}</Button>}
    />
    <ApiErrorAlert error={error} />
    {error ? null : isLoading ? <p className="text-sm text-muted-foreground">{t("common.loading")}</p> : credentials.length === 0 ? <p className="rounded-xl border border-dashed border-border-strong p-5 text-[13px] text-muted-foreground">{t("gitCredentials.emptyState")}</p> : <ul className="flex flex-col overflow-hidden rounded-xl border border-border">{credentials.map((credential, index) => <li key={credential.host_owner} className={`flex flex-wrap items-center justify-between gap-2 px-5 py-3.5 ${index === 0 ? "" : "border-t border-border"}`}><div className="flex flex-wrap items-center gap-2"><code className="font-mono text-sm font-medium">{credential.host_owner}</code><span className="text-xs text-muted-foreground">{t("gitCredentials.updatedAt")}: {formatUtcTimestamp(credential.updated_at)}</span>{credential.rejected_at != null ? <Badge variant="destructive">{t("gitCredentials.rejected")}</Badge> : null}{(credential.app_ids ?? []).map((appId) => <Badge key={appId} variant="outline">{appId}</Badge>)}</div><div className="flex gap-1"><Button variant="outline" size="sm" onClick={() => setDialog({ hostOwner: credential.host_owner })}>{t("gitCredentials.updateButton")}</Button><Button variant="dangerGhost" size="sm" onClick={() => setDeleteTarget(credential)}>{t("common.delete")}</Button></div></li>)}</ul>}
    <CredentialDialog dialog={dialog} credentials={credentials} onClose={() => setDialog(null)} onSaved={invalidate} />
    <AlertDialog open={deleteTarget !== null} onOpenChange={(open) => !open && setDeleteTarget(null)}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{t("gitCredentials.deleteTitle", { hostOwner: deleteTarget?.host_owner ?? "" })}</AlertDialogTitle></AlertDialogHeader><ApiErrorAlert error={deletion.error} /><AlertDialogFooter><AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel><Button variant="destructive" disabled={deletion.isPending} onClick={() => deleteTarget && deletion.mutate({ hostOwner: deleteTarget.host_owner })}>{t("common.delete")}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog>
  </section>;
}

function CredentialDialog({ dialog, credentials, onClose, onSaved }: { dialog: Dialog; credentials: GitCredentialRecordInfo[]; onClose: () => void; onSaved: () => void }) {
  const { t } = useTranslation();
  const [hostOwner, setHostOwner] = useState("");
  const [token, setToken] = useState("");
  const put = usePutGitCredentialApiV1GitCredentialsHostOwnerPut({ mutation: { onSuccess: () => { onSaved(); onClose(); setHostOwner(""); setToken(""); } } });
  const effectiveHostOwner = dialog?.hostOwner ?? hostOwner;
  const replacing = !dialog?.hostOwner && credentials.some((credential) => normalizedHostOwner(credential.host_owner) === normalizedHostOwner(effectiveHostOwner));
  return <AlertDialog open={dialog !== null} onOpenChange={(open) => !open && onClose()}><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{dialog?.hostOwner ? t("gitCredentials.updateTitle", { hostOwner: dialog.hostOwner }) : t("gitCredentials.addTitle")}</AlertDialogTitle>{!dialog?.hostOwner ? <AlertDialogDescription>{t("gitCredentials.hostOwnerHint")}</AlertDialogDescription> : null}</AlertDialogHeader><div className="flex flex-col gap-3">{dialog?.hostOwner ? <p className="font-mono text-sm">{dialog.hostOwner}</p> : <div className="flex flex-col gap-1"><Label htmlFor="git-host-owner">{t("gitCredentials.hostOwnerLabel")}</Label><Input id="git-host-owner" value={hostOwner} onChange={(event) => setHostOwner(event.target.value)} /></div>}<div className="flex flex-col gap-1"><Label htmlFor="git-token">{t("gitCredentials.tokenLabel")}</Label><Input id="git-token" type="password" value={token} onChange={(event) => setToken(event.target.value)} /></div>{replacing ? <p className="text-sm text-muted-foreground">{t("gitCredentials.replacing")}</p> : null}<ApiErrorAlert error={put.error} /></div><AlertDialogFooter><AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel><Button disabled={put.isPending || !effectiveHostOwner.trim() || !token} onClick={() => put.mutate({ hostOwner: effectiveHostOwner, data: { value: token } })}>{replacing ? t("gitCredentials.replaceButton") : t("common.save")}</Button></AlertDialogFooter></AlertDialogContent></AlertDialog>;
}
