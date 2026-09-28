/**
 * Copies `text` to the clipboard, returning whether it worked. The Portal is served over plain
 * http on the LAN (no TLS cert for a `.local` device hostname), which is not a secure context,
 * so `navigator.clipboard` is `undefined` there and a bare `writeText` call silently does
 * nothing -- this falls back to the `execCommand("copy")` textarea trick in that case.
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    textarea.remove();
  }
}
