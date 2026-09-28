/** The app id's name part (design doc 3.9) -- mirrors the backend's `_validate_requested_name`
 * (`palmimo_portal/api/apps.py`) so an invalid value is caught before a round trip. */
export const APP_NAME_PART_RE = /^[a-z][a-z0-9-]{0,39}$/;

export function isValidAppNamePart(value: string): boolean {
  return APP_NAME_PART_RE.test(value);
}
