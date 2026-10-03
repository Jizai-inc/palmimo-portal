function parseContainer(text: string): unknown {
  const value: unknown = JSON.parse(text);
  return value !== null && typeof value === "object" ? value : undefined;
}

function expand(value: unknown): unknown {
  if (typeof value === "string") {
    try {
      const parsed = parseContainer(value);
      return parsed === undefined ? value : expand(parsed);
    } catch {
      return value;
    }
  }
  if (Array.isArray(value)) return value.map(expand);
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, expand(item)]));
  }
  return value;
}

export function formatLogMessage(message: string, pretty: boolean): string {
  try {
    const value = parseContainer(message);
    return value === undefined ? message : JSON.stringify(expand(value), null, pretty ? 2 : undefined);
  } catch {
    return message;
  }
}
