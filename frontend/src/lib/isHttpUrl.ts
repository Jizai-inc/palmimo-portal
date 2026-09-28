/**
 * Whether `url` is safe to render as a clickable link -- an `http:`/`https:` scheme only.
 *
 * The backend rejects non-http(s) `help_url` values at manifest-parse and env-endpoint
 * validation time, but a manifest that predates that validation (or a future backend
 * regression) must not let the frontend turn a `javascript:` URL into a followable link.
 */
export function isHttpUrl(url: string | null | undefined): url is string {
  if (!url) return false;
  try {
    return ["http:", "https:"].includes(new URL(url).protocol);
  } catch {
    return false;
  }
}
