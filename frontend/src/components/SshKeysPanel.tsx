import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Copy, Download, KeyRound, TriangleAlert, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getListKeysApiV1SshKeysGetQueryKey,
  useAddKeyApiV1SshKeysPost,
  useDeleteKeyApiV1SshKeysFingerprintDelete,
  useListKeysApiV1SshKeysGet,
} from "@/api/generated/ssh-keys/ssh-keys";
import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
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
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { generateEd25519KeyPair, probeEd25519KeygenSupport } from "@/lib/sshKeygen";

const LAST_KEY_CONFIRMATION = "last-key";
const PRIVATE_KEY_FILENAME = "palmimo_ed25519";
// Kept identical to the quickstart guide's step 3, so the printed sheet and the screen agree.
const INSTALL_COMMANDS = `mv ~/Downloads/${PRIVATE_KEY_FILENAME} ~/.ssh/\nchmod 600 ~/.ssh/${PRIVATE_KEY_FILENAME}`;
// How long one download's Blob URL stays alive: long enough for a slow download-start to
// land, short enough not to leak. Revoking any earlier would cancel the transfer itself.
const DOWNLOAD_URL_LIFETIME_MS = 30_000;

/** Which of the two ways of adding a key the user picked; `null` while the choice is still open. */
type AddMode = "generate" | "register";

/** Folds away everything the server's `parse_authorized_key` would reject or read as a smuggled second line. */
function sanitizeComment(comment: string): string {
  return comment.replace(/\s+/g, " ").trim();
}

/** The comment carried by a single-line `authorized_keys` entry: everything past the type and the blob. */
function commentOf(publicKeyLine: string): string {
  const line = publicKeyLine.trim();
  if (/[\r\n]/.test(line)) return "";
  return line.split(/\s+/).slice(2).join(" ");
}

/**
 * `publicKeyLine` with its comment replaced by `comment`.
 *
 * Anything that is not one parseable `type blob ...` line is passed through untouched, leaving
 * the verdict to the server: rebuilding a multi-line paste from its first two fields would turn
 * the server's rejection of it into a silent registration of only its first key.
 */
function withComment(publicKeyLine: string, comment: string): string {
  const line = publicKeyLine.trim();
  const parts = line.split(/\s+/);
  if (parts.length < 2 || /[\r\n]/.test(line)) return line;
  return `${parts[0]} ${parts[1]} ${sanitizeComment(comment)}`.trim();
}

/**
 * Offers `privateKeyFile` to the browser as a download of {@link PRIVATE_KEY_FILENAME}.
 *
 * Each call mints its own Blob URL rather than reusing one: the notice tells the user to go
 * looking in their downloads folder, which routinely takes longer than any URL kept alive on a
 * timer, and a retry against a revoked URL fails silently -- leaving no way to get the key.
 */
function downloadPrivateKey(privateKeyFile: string) {
  const url = URL.createObjectURL(new Blob([privateKeyFile], { type: "application/octet-stream" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = PRIVATE_KEY_FILENAME;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_URL_LIFETIME_MS);
}

/** A generated key pair awaiting the user's explicit "I saved it" confirmation before its public half is filled in and made registerable. */
interface PendingGeneratedKey {
  publicKeyLine: string;
  privateKeyFile: string;
}

/** How long the copy button shows its "copied" checkmark before reverting. */
const COPIED_RESET_MS = 2000;

/**
 * The ready-to-run SSH command, built from the device's own hostname. Renders nothing until
 * the hostname loads, so it never flashes `ssh user@undefined.local`.
 *
 * It names the private key explicitly: a key generated here lands under a non-default file
 * name, which bare `ssh user@host` never offers and the server then rejects as `publickey`.
 */
function SshCommandHint() {
  const { t } = useTranslation();
  const { data: systemStatus } = useGetStatusApiV1SystemStatusGet();
  const [copied, setCopied] = useState(false);

  if (!systemStatus?.hostname) return null;

  const command = `ssh -i ~/.ssh/${PRIVATE_KEY_FILENAME} user@${systemStatus.hostname}.local`;

  async function handleCopy() {
    await navigator.clipboard.writeText(command);
    setCopied(true);
    setTimeout(() => setCopied(false), COPIED_RESET_MS);
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">{t("sshKeys.sshCommandLabel")}</span>
        <code className="rounded-md border border-input bg-muted px-2 py-1 font-mono text-xs break-all">{command}</code>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={copied ? t("sshKeys.copiedCommand") : t("sshKeys.copyCommand")}
          onClick={() => void handleCopy()}
        >
          {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">{t("sshKeys.sshCommandNote")}</p>
    </div>
  );
}

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
  const [canGenerateKey, setCanGenerateKey] = useState<boolean | null>(null);
  const { data: keys, isLoading, error: listError } = useListKeysApiV1SshKeysGet();
  const [mode, setMode] = useState<AddMode | null>(null);
  const [publicKey, setPublicKey] = useState("");
  const [comment, setComment] = useState("");
  const [commentEdited, setCommentEdited] = useState(false);
  const [dialog, setDialog] = useState<DeleteDialogState | null>(null);
  const [pendingKey, setPendingKey] = useState<PendingGeneratedKey | null>(null);
  const [justConfirmed, setJustConfirmed] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [generateError, setGenerateError] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    void probeEd25519KeygenSupport().then((supported) => {
      if (!cancelled) setCanGenerateKey(supported);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  function applyPublicKey(line: string) {
    setPublicKey(line);
    if (!commentEdited) setComment(commentOf(line));
  }

  async function handleFileChosen(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    applyPublicKey((await file.text()).trim());
  }

  async function handleGenerateKey() {
    setIsGenerating(true);
    setGenerateError(false);
    try {
      const { publicKeyLine, privateKeyFile } = await generateEd25519KeyPair(sanitizeComment(comment));
      // Best-effort convenience: a browser can silently ignore or delay a synthetic click, so
      // this is never treated as proof the file was saved -- only the user's own confirmation
      // below (handleConfirmSaved) unlocks registering the matching public key.
      downloadPrivateKey(privateKeyFile);
      setPendingKey({ publicKeyLine, privateKeyFile });
    } catch {
      setGenerateError(true);
    } finally {
      setIsGenerating(false);
    }
  }

  function handleRedownloadPendingKey() {
    if (!pendingKey) return;
    downloadPrivateKey(pendingKey.privateKeyFile);
  }

  function handleConfirmSaved() {
    if (!pendingKey) return;
    setPublicKey(pendingKey.publicKeyLine);
    setPendingKey(null);
    setJustConfirmed(true);
  }

  function resetAddForm() {
    setMode(null);
    setPublicKey("");
    setComment("");
    setCommentEdited(false);
    setJustConfirmed(false);
    setGenerateError(false);
  }

  const addKey = useAddKeyApiV1SshKeysPost({
    mutation: {
      onSuccess: () => {
        resetAddForm();
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

  // Without keygen support there is nothing to choose between, so the register branch is the
  // whole screen; `null` means the probe has not answered yet and neither branch can be drawn.
  const activeMode: AddMode | null = canGenerateKey === null ? null : canGenerateKey ? mode : "register";
  const namedComment = sanitizeComment(comment);
  const generatedKeyExists = pendingKey !== null || justConfirmed;

  function handleBack() {
    resetAddForm();
    addKey.reset();
  }

  function handleAddSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!publicKey.trim() || addKey.isPending) return;
    // An untouched name field means the pasted line keeps its own comment; once the field has
    // been edited its value wins, including an empty one that drops the comment entirely.
    const line = activeMode === "register" && commentEdited ? withComment(publicKey, namedComment) : publicKey.trim();
    addKey.mutate({ data: { public_key: line } });
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

  // The name is locked from the moment generation starts: handleGenerateKey has already captured
  // it, so a later edit would name the key on screen something the key itself is not.
  const nameIsLocked = activeMode === "generate" && (isGenerating || generatedKeyExists);

  const keyNameField = (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor="ssh-key-comment">{t("sshKeys.commentLabel")}</Label>
      <Input
        id="ssh-key-comment"
        autoComplete="off"
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        placeholder={t("sshKeys.commentPlaceholder")}
        value={comment}
        disabled={nameIsLocked}
        onChange={(event) => {
          setComment(event.target.value);
          setCommentEdited(true);
        }}
      />
      <p className="text-xs text-muted-foreground">{t("sshKeys.commentHelp")}</p>
    </div>
  );

  const addForm = (
    <form className="flex flex-col gap-2" onSubmit={handleAddSubmit}>
      <Label htmlFor="ssh-public-key">{t("sshKeys.publicKeyLabel")}</Label>
      {activeMode === "register" ? (
        <>
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
        </>
      ) : null}
      <Textarea
        id="ssh-public-key"
        placeholder={t("sshKeys.publicKeyPlaceholder")}
        spellCheck={false}
        autoComplete="off"
        value={publicKey}
        onChange={(event) => applyPublicKey(event.target.value)}
      />
      {activeMode === "register" ? (
        <>
          <p className="text-xs text-muted-foreground">{t("sshKeys.uploadHelp")}</p>
          {keyNameField}
        </>
      ) : null}
      <ApiErrorAlert error={addKey.error} />
      <Button type="submit" disabled={addKey.isPending || !publicKey.trim()}>
        {t("sshKeys.addSubmit")}
      </Button>
    </form>
  );

  const branchChoice = (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
      <p className="text-sm font-semibold">{t("sshKeys.addChoiceTitle")}</p>
      <div className="flex flex-col gap-2 md:flex-row">
        <Button
          type="button"
          variant="outline"
          className="h-auto flex-1 flex-col items-start justify-start gap-1 whitespace-normal py-3 text-left"
          onClick={() => setMode("generate")}
        >
          <span className="flex items-center gap-2 font-semibold">
            <KeyRound className="size-4" />
            {t("sshKeys.chooseGenerate")}
          </span>
          <span className="text-xs font-normal text-muted-foreground">{t("sshKeys.chooseGenerateHelp")}</span>
        </Button>
        <Button
          type="button"
          variant="outline"
          className="h-auto flex-1 flex-col items-start justify-start gap-1 whitespace-normal py-3 text-left"
          onClick={() => setMode("register")}
        >
          <span className="flex items-center gap-2 font-semibold">
            <Upload className="size-4" />
            {t("sshKeys.chooseRegister")}
          </span>
          <span className="text-xs font-normal text-muted-foreground">{t("sshKeys.chooseRegisterHelp")}</span>
        </Button>
      </div>
    </div>
  );

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">{t("sshKeys.body")}</p>
      <SshCommandHint />
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

      {listError || canGenerateKey === null ? null : activeMode === null ? (
        branchChoice
      ) : (
        <div className="flex flex-col gap-4 rounded-xl border border-border bg-card p-4">
          {/* Withheld only while an unconfirmed private key is on screen, so leaving cannot strand
            a file the user has not decided about yet. It comes back once they confirm: starting
            over is then the only way out for a download that never actually landed. */}
          {canGenerateKey && !isGenerating && pendingKey === null ? (
            <Button type="button" variant="ghost" size="sm" className="-ml-2 w-fit" onClick={handleBack}>
              <ArrowLeft className="size-4" />
              {t("common.back")}
            </Button>
          ) : null}

          {activeMode === "generate" ? (
            <>
              {keyNameField}
              {pendingKey ? (
                <>
                  <Alert>
                    <TriangleAlert />
                    <AlertTitle>{t("sshKeys.generatedNoteTitle")}</AlertTitle>
                    <AlertDescription className="flex flex-col gap-2">
                      <span className="whitespace-pre-line">
                        {t("sshKeys.generatedNoteDownloaded", { filename: PRIVATE_KEY_FILENAME })}
                      </span>
                      <span>{t("sshKeys.generatedNoteInstallIntro")}</span>
                      <code className="overflow-x-auto whitespace-pre rounded-md border border-input bg-muted px-3 py-2 font-mono text-xs text-foreground">
                        {INSTALL_COMMANDS}
                      </code>
                      <span>{t("sshKeys.generatedNoteSafety")}</span>
                    </AlertDescription>
                  </Alert>
                  <div className="flex flex-wrap gap-2">
                    <Button type="button" variant="outline" onClick={handleRedownloadPendingKey}>
                      <Download className="size-4" />
                      <span className="font-semibold">{t("sshKeys.downloadAgainButton")}</span>
                    </Button>
                    <Button type="button" onClick={handleConfirmSaved}>
                      {t("sshKeys.confirmSavedButton")}
                    </Button>
                  </div>
                </>
              ) : justConfirmed ? null : (
                <Button
                  type="button"
                  variant="outline"
                  className="w-fit"
                  onClick={() => void handleGenerateKey()}
                  disabled={isGenerating || !namedComment}
                >
                  <KeyRound className="size-4" />
                  <span className="font-semibold">{t("sshKeys.generateButton")}</span>
                </Button>
              )}
              {generateError ? (
                <Alert variant="destructive">
                  <TriangleAlert />
                  <AlertDescription>{t("sshKeys.generateErrorMessage")}</AlertDescription>
                </Alert>
              ) : null}
              {justConfirmed ? (
                <Alert>
                  <AlertDescription>{t("sshKeys.confirmedNoteBody")}</AlertDescription>
                </Alert>
              ) : null}
            </>
          ) : null}

          {activeMode === "register" || justConfirmed ? addForm : null}
        </div>
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
