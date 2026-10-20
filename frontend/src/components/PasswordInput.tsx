import { Eye, EyeOff } from "lucide-react";
import { useState } from "react";
import type * as React from "react";
import { useTranslation } from "react-i18next";

import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/** A password `Input` with a show/hide toggle, so a typo can be caught before submitting. */
export function PasswordInput({ className, ...props }: Omit<React.ComponentProps<"input">, "type">) {
  const { t } = useTranslation();
  const [visible, setVisible] = useState(false);

  return (
    <div className="relative">
      {/* Shown as text, the field would otherwise get mobile autocapitalize/autocorrect and spellcheck. */}
      <Input
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        {...props}
        type={visible ? "text" : "password"}
        className={cn("pr-10", className)}
      />
      <button
        type="button"
        onClick={() => setVisible((shown) => !shown)}
        aria-pressed={visible}
        aria-label={t("common.togglePasswordVisibility")}
        className="absolute inset-y-0 right-0 flex w-10 items-center justify-center text-muted-foreground hover:text-foreground"
      >
        {visible ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
      </button>
    </div>
  );
}
