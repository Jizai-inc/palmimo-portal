import { useQueryClient } from "@tanstack/react-query";
import { TriangleAlert, Trash2, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getListKeysApiV1SshKeysGetQueryKey,
  useAddKeyApiV1SshKeysPost,
  useDeleteKeyApiV1SshKeysFingerprintDelete,
  useListKeysApiV1SshKeysGet,
} from "@/api/generated/ssh-keys/ssh-keys";
import type { SshKeyResponse } from "@/api/generated/models";
import { PortalApiError } from "@/api/client";
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
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

const LAST_KEY_CONFIRMATION = "last-key";

/** Which key the delete confirmation dialog is open for, and whether it is in last-key (lockout-warning) mode. */
interface DeleteDialogState {
  key: SshKeyResponse;
  lastKeyMode: boolean;
}

/**
 * The SSH-keys screen's logic (see routes/ssh-keys.tsx, which is just this inside `AppShell`).
 * Free of router hooks so it can be rendered directly in tests.
 */
export function SshKeysPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: keys, isLoading, error: listError } = useListKeysApiV1SshKeysGet();
  const [publicKey, setPublicKey] = useState("");
  const [dialog, setDialog] = useState<DeleteDialogState | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  async function handleFileChosen(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setPublicKey((await file.text()).trim());
  }

  const addKey = useAddKeyApiV1SshKeysPost({
    mutation: {
      onSuccess: () => {
        setPublicKey("");
        void queryClient.invalidateQueries({ queryKey: getListKeysApiV1SshKeysGetQueryKey() });
      },
    },
  });

  const deleteKey = useDeleteKeyApiV1SshKeysFingerprintDelete({
    mutation: {
      onSuccess: () => {
        setDialog(null);
        void queryClient.invalidateQueries({ queryKey: getListKeysApiV1SshKeysGetQueryKey() });
      },
      onError: (error) => {
        // A race: another session deleted the other keys since our last list refresh, so the
        // server now also sees this as the last key. Re-open the dialog in last-key mode.
        setDialog((current) => {
          if (current && error instanceof PortalApiError && error.code === "last_key_deletion_requires_confirmation") {
            return { key: current.key, lastKeyMode: true };
          }
          return null;
        });
      },
    },
  });

  function handleAddSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!publicKey.trim() || addKey.isPending) return;
    addKey.mutate({ data: { public_key: publicKey } });
  }

  function openDeleteDialog(key: SshKeyResponse) {
    deleteKey.reset();
    setDialog({ key, lastKeyMode: (keys ?? []).length === 1 });
  }

  function confirmDelete() {
    if (!dialog) return;
    deleteKey.mutate({
      fingerprint: dialog.key.fingerprint,
      params: dialog.lastKeyMode ? { confirm: LAST_KEY_CONFIRMATION } : undefined,
    });
  }

  // The 409 race case reopens the dialog instead of surfacing an inline error, so only render
  // the inline alert once the dialog is closed.
  const showDeleteError = !dialog && deleteKey.isError;

  function closeDialog() {
    setDialog(null);
    deleteKey.reset(); // clears a stale 409 error so showDeleteError doesn't surface it later
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">{t("sshKeys.body")}</p>
      <ApiErrorAlert error={listError} />
      {showDeleteError ? <ApiErrorAlert error={deleteKey.error} /> : null}

      {/* A list error means we don't know the real key list -- render neither empty state nor form. */}
      {listError ? null : isLoading ? (
        <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
      ) : (keys ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("sshKeys.emptyState")}</p>
      ) : (
        <div className="overflow-hidden rounded-md border border-input">
          {/* Desktop-only column header. */}
          <div className="hidden grid-cols-[1fr_120px_1fr_44px] gap-2 bg-muted px-3 py-2 text-xs font-medium text-muted-foreground md:grid">
            <span>{t("sshKeys.columnComment")}</span>
            <span>{t("sshKeys.columnType")}</span>
            <span>{t("sshKeys.columnFingerprint")}</span>
            <span aria-hidden />
          </div>
          <ul className="flex flex-col">
            {(keys ?? []).map((key, index) => (
              <li
                key={key.fingerprint}
                className={`flex flex-col gap-1 p-3 md:grid md:grid-cols-[1fr_120px_1fr_44px] md:items-center md:gap-2 md:px-3 md:py-2 ${index === 0 ? "" : "border-t border-input"}`}
              >
                <span className="min-w-0 max-w-full truncate text-sm font-medium">{key.comment || t("sshKeys.noComment")}</span>
                <Badge variant="outline" className="w-fit">
                  {key.key_type}
                </Badge>
                <span className="break-all font-mono text-xs text-muted-foreground">{key.fingerprint}</span>
                <Button
                  variant="outline"
                  size="icon"
                  className="w-fit self-end md:self-auto md:justify-self-end"
                  aria-label={t("sshKeys.delete")}
                  onClick={() => openDeleteDialog(key)}
                >
                  <Trash2 className="size-4 text-destructive" />
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {listError ? null : (
        <form className="flex flex-col gap-2 rounded-xl border border-border bg-card p-4 md:p-0 md:border-0 md:bg-transparent" onSubmit={handleAddSubmit}>
          <p className="hidden text-sm font-semibold md:block">{t("sshKeys.addCardTitle")}</p>
          <Label htmlFor="ssh-public-key">{t("sshKeys.publicKeyLabel")}</Label>
          <div className="flex items-center gap-2">
            <Button type="button" variant="outline" onClick={() => fileInputRef.current?.click()}>
              <Upload className="size-4" />
              <span className="font-semibold">{t("sshKeys.chooseFileButton")}</span>
            </Button>
            <span className="text-xs text-muted-foreground">{t("sshKeys.orPasteBelow")}</span>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            accept=".pub,text/plain"
            className="hidden"
            onChange={(event) => void handleFileChosen(event)}
            aria-label={t("sshKeys.chooseFileButton")}
          />
          <Textarea
            id="ssh-public-key"
            placeholder={t("sshKeys.publicKeyPlaceholder")}
            spellCheck={false}
            autoComplete="off"
            value={publicKey}
            onChange={(event) => setPublicKey(event.target.value)}
          />
          <p className="text-xs text-muted-foreground">{t("sshKeys.uploadHelp")}</p>
          <ApiErrorAlert error={addKey.error} />
          <Button type="submit" disabled={addKey.isPending || !publicKey.trim()}>
            {t("sshKeys.addSubmit")}
          </Button>
        </form>
      )}

      <AlertDialog open={dialog !== null} onOpenChange={(open) => !open && closeDialog()}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{dialog?.lastKeyMode ? t("sshKeys.lastKeyDialogTitle") : t("sshKeys.deleteDialogTitle")}</AlertDialogTitle>
            {dialog?.lastKeyMode ? null : (
              <AlertDialogDescription>
                {t("sshKeys.deleteDialogBody", {
                  comment: dialog?.key.comment || t("sshKeys.noComment"),
                  fingerprint: dialog?.key.fingerprint ?? "",
                })}
              </AlertDialogDescription>
            )}
          </AlertDialogHeader>
          {dialog?.lastKeyMode ? (
            <div className="flex items-start gap-2 rounded-md bg-muted p-3 text-sm text-muted-foreground">
              <TriangleAlert className="mt-0.5 size-4 shrink-0 text-destructive" />
              <p>{t("sshKeys.lastKeyDialogBody")}</p>
            </div>
          ) : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteKey.isPending}>{t("common.cancel")}</AlertDialogCancel>
            {/* A plain Button, not `AlertDialogAction`: that closes synchronously via Radix's
              `Dialog.Close`, which would race the 409 last-key confirmation's `onError` reopen. */}
            <Button variant="destructive" onClick={confirmDelete} disabled={deleteKey.isPending}>
              {dialog?.lastKeyMode ? t("sshKeys.lastKeyDialogConfirm") : t("sshKeys.deleteDialogConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
