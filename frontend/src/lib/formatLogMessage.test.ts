import { expect, it } from "vitest";

import { formatLogMessage } from "./formatLogMessage";

it.each([
  ["plain text", false, "plain text"],
  ['prefix {"a":1}', true, 'prefix {"a":1}'],
  ["123", false, "123"],
  ['"abc"', true, '"abc"'],
  ["true", false, "true"],
  ['{"reason":"\\u58c1\\u306e\\u82b1\\u67c4"}', false, '{"reason":"壁の花柄"}'],
  ['{"arguments":"{\\"a\\":1}"}', false, '{"arguments":"{\\"a\\":1}"}'],
  ['[{"a":1}]', true, '[\n  {\n    "a": 1\n  }\n]'],
])("formats %s with pretty=%s", (message, pretty, expected) => {
  expect(formatLogMessage(message, pretty)).toBe(expected);
});
