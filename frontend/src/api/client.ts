// The fetcher orval's generated hooks call into (orval.config.ts's
// `override.mutator`) -- the only hand-written HTTP call in the frontend.

/** The `{"error": {"code", "params"}}` envelope every Portal endpoint answers errors with. */
export interface PortalErrorBody {
  error: {
    code: string;
    params: Record<string, unknown>;
  };
}

/** A failed Portal API call, carrying the machine-readable error code i18n resolves. */
export class PortalApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly params: Record<string, unknown>;

  constructor(status: number, code: string, params: Record<string, unknown>) {
    super(`Portal API error ${status}: ${code}`);
    this.name = "PortalApiError";
    this.status = status;
    this.code = code;
    this.params = params;
  }
}

const CSRF_HEADER_NAME = "X-Requested-With";
const CSRF_HEADER_VALUE = "PalmimoPortal";
const STATE_CHANGING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function isPortalErrorBody(value: unknown): value is PortalErrorBody {
  if (typeof value !== "object" || value === null || !("error" in value)) {
    return false;
  }
  const error = (value as { error: unknown }).error;
  return typeof error === "object" && error !== null && "code" in error;
}

/**
 * orval's fetch-client mutator: called with a request's URL and `RequestInit`,
 * returns the parsed response body. Adds the two things every Portal request
 * needs (per palmimo-portal-technical.md's CSRF / session sections) and
 * translates a non-2xx response into a typed {@link PortalApiError} instead of
 * letting callers deal with `Response` objects and the raw error envelope.
 */
export async function customFetch<T>(url: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (STATE_CHANGING_METHODS.has(method)) {
    // A cross-site form submission cannot attach a custom header, so this
    // is what CSRFMiddleware (palmimo_portal/app.py) checks for.
    headers.set(CSRF_HEADER_NAME, CSRF_HEADER_VALUE);
  }

  const response = await fetch(url, {
    ...options,
    method,
    headers,
    // The session cookie is HttpOnly + SameSite=Strict; same-origin requests
    // send it automatically, but the dev server proxies /api to a different
    // origin (see vite.config.ts), so this is required there too.
    credentials: "include",
  });

  const contentType = response.headers.get("content-type") ?? "";
  const body: unknown = contentType.includes("application/json") ? await response.json() : await response.text();

  if (!response.ok) {
    if (isPortalErrorBody(body)) {
      throw new PortalApiError(response.status, body.error.code, body.error.params);
    }
    throw new PortalApiError(response.status, "http_error", { detail: body });
  }

  return body as T;
}

export default customFetch;
