/** Re-serializes a message that is wholly a JSON object or array; strings nested inside stay as they are. */
export function formatLogMessage(message: string, pretty: boolean): string {
  try {
    const value: unknown = JSON.parse(message);
    if (value === null || typeof value !== "object") return message;
    return JSON.stringify(value, null, pretty ? 2 : undefined);
  } catch {
    return message;
  }
}
