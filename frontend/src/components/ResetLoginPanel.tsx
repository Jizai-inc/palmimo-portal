import { useQueryClient } from "@tanstack/react-query";
import { Check, RotateCcw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useResetApiV1AuthResetPost } from "@/api/generated/auth/auth";
import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { CenteredState } from "@/components/CenteredState";
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

/**
 * The login-credentials-reset screen's logic (see routes/reset-login.tsx, which wraps this in
 * `AuthShell`). `POST /auth/reset` is reachable unauthenticated and unprovisioned -- the escape
 * hatch for a forgotten owner-set password on an identity-carrying device, which would
 * otherwise block Wi-Fi setup itself.
 */
export function ResetLoginPanel({ onBack, onDone }: { onBack: () => void; onDone: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: status } = useGetStatusApiV1SystemStatusGet();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [succeeded, setSucceeded] = useState(false);

  const mutation = useResetApiV1AuthResetPost({
    mutation: {
      onSuccess: () => {
        setDialogOpen(false);
        setSucceeded(true);
        // No screen after this point may keep rendering pre-reset auth state from cache.
        queryClient.clear();
      },
      onError: () => setDialogOpen(false),
    },
  });

  if (succeeded) {
    return (
      <CenteredState
        icon={RotateCcw}
        title={t("resetLogin.successTitle")}
        body={t("resetLogin.successBody")}
        action={
          <Button className="w-full" onClick={onDone}>
            {t("resetLogin.successAction")}
          </Button>
        }
      />
    );
  }

  const hostname = status?.hostname ?? "";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2 rounded-xl bg-muted p-4">
        <PointRow text={t("resetLogin.point1")} />
        <PointRow text={t("resetLogin.point2")} />
        <PointRow text={t("resetLogin.point3")} />
      </div>
      <div className="flex items-center gap-2 text-sm">
        <span className="text-muted-foreground">{t("resetLogin.deviceLabel")}</span>
        <Badge variant="outline">{hostname}</Badge>
      </div>
      <ApiErrorAlert error={mutation.error} />
      <div className="flex flex-col gap-3 md:flex-row">
        <Button variant="outline" className="flex-1" onClick={onBack}>
          {t("common.back")}
        </Button>
        <Button variant="destructive" className="flex-1" onClick={() => setDialogOpen(true)}>
          {t("resetLogin.resetButton")}
        </Button>
      </div>

      <AlertDialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("resetLogin.dialogTitle")}</AlertDialogTitle>
            <AlertDialogDescription>{t("resetLogin.dialogBody")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={mutation.isPending}>{t("common.cancel")}</AlertDialogCancel>
            <Button variant="destructive" disabled={mutation.isPending} onClick={() => mutation.mutate()}>
              {t("resetLogin.dialogConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function PointRow({ text }: { text: string }) {
  return (
    <div className="flex items-start gap-2 text-sm">
      <Check className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
      <span>{text}</span>
    </div>
  );
}
