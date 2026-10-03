import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { LogViewer } from "./LogViewer";

const first = [{ message: "first", timestamp: null, invocation_id: null }];
const next = [...first, { message: "second", timestamp: null, invocation_id: null }];

beforeEach(() => localStorage.clear());

function scrollWindow() {
  const node = screen.getByRole("region", { name: "Logs" });
  Object.defineProperties(node, {
    scrollHeight: { configurable: true, value: 1000 },
    clientHeight: { configurable: true, value: 200 },
    scrollTop: { configurable: true, writable: true, value: 800 },
  });
  return node;
}

function followToggle() {
  fireEvent.click(screen.getByRole("button", { name: "Display" }));
  return screen.getByRole("menuitemcheckbox", { name: "Follow latest" });
}

it("scrolls to the bottom when new entries arrive while following", () => {
  const { rerender } = render(<LogViewer entries={first} text="first" />);
  const node = scrollWindow();
  rerender(<LogViewer entries={next} text="first\nsecond" />);
  expect(node.scrollTop).toBe(1000);
});

it("stops following after scrolling up and preserves the position on new entries", () => {
  const { rerender } = render(<LogViewer entries={first} text="first" />);
  const node = scrollWindow();
  node.scrollTop = 400;
  fireEvent.scroll(node);
  expect(followToggle()).toHaveAttribute("aria-checked", "false");
  rerender(<LogViewer entries={next} text="first\nsecond" />);
  expect(node.scrollTop).toBe(400);
});

it("resumes following after manually scrolling to the bottom", () => {
  render(<LogViewer entries={first} text="first" />);
  const node = scrollWindow();
  node.scrollTop = 400;
  fireEvent.scroll(node);
  node.scrollTop = 798;
  fireEvent.scroll(node);
  expect(followToggle()).toHaveAttribute("aria-checked", "true");
});

it("renders defaults when localStorage is unavailable", () => {
  const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
  const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
  try {
    render(<LogViewer entries={first} text="first" />);
    fireEvent.click(screen.getByRole("button", { name: "Display" }));
    expect(screen.getByRole("menuitemcheckbox", { name: "Wrap lines" })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Pretty JSON" }));
    expect(screen.getByText("first")).toBeInTheDocument();
  } finally {
    get.mockRestore();
    set.mockRestore();
  }
});

it("keeps Safari menu clicks working after a blur with no related target", () => {
  render(<LogViewer entries={first} text="first" />);
  fireEvent.click(screen.getByRole("button", { name: "Display" }));
  const wrap = screen.getByRole("menuitemcheckbox", { name: "Wrap lines" });
  wrap.focus();
  fireEvent.blur(wrap, { relatedTarget: null });
  fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Follow latest" }));
  expect(screen.getByRole("menuitemcheckbox", { name: "Follow latest" })).toHaveAttribute("aria-checked", "false");
  fireEvent.pointerDown(document.body);
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
});

it("preserves the visible row when capped history drops its oldest entry", () => {
  const entries = Array.from({ length: 2000 }, (_, i) => ({ message: `line-${i}`, timestamp: null, invocation_id: null }));
  const { rerender } = render(<LogViewer entries={entries} text="" droppedCount={0} />);
  const node = scrollWindow();
  const topDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetTop")!;
  const heightDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight")!;
  Object.defineProperty(HTMLElement.prototype, "offsetTop", { configurable: true, get() {
    const text = this.textContent ?? "";
    const index = Number(text.match(/line-(\d+)/)?.[1] ?? 0);
    return (index - dropped) * 20;
  } });
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 20 });
  let dropped = 0;
  try {
    node.scrollTop = 405;
    fireEvent.scroll(node);
    const row = screen.getByText("line-20").parentElement!;
    expect(row.offsetTop - node.scrollTop).toBe(-5);
    dropped = 1;
    rerender(<LogViewer entries={[...entries.slice(1), { message: "line-2000", timestamp: null, invocation_id: null }]} text="" droppedCount={1} />);
    expect(screen.getByText("line-20").parentElement!.offsetTop - node.scrollTop).toBe(-5);
  } finally {
    Object.defineProperty(HTMLElement.prototype, "offsetTop", topDescriptor);
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", heightDescriptor);
  }
});

it("resumes following when the invocation changes", () => {
  const { rerender } = render(<LogViewer entries={first} text="first" invocation="old" />);
  const node = scrollWindow();
  node.scrollTop = 400;
  fireEvent.scroll(node);
  rerender(<LogViewer entries={next} text="first\nsecond" invocation="new" />);
  expect(node.scrollTop).toBe(1000);
  expect(followToggle()).toHaveAttribute("aria-checked", "true");
});

it("jumps to the bottom when following is enabled in the menu", () => {
  render(<LogViewer entries={first} text="first" />);
  const node = scrollWindow();
  node.scrollTop = 400;
  fireEvent.scroll(node);
  fireEvent.click(followToggle());
  expect(node.scrollTop).toBe(1000);
});

it("places the next surviving row at the anchor position when the anchor is dropped", () => {
  const entries = Array.from({ length: 2000 }, (_, i) => ({ message: `line-${i}`, timestamp: null, invocation_id: null }));
  const { rerender } = render(<LogViewer entries={entries} text="" droppedCount={0} />);
  const node = scrollWindow();
  const topDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetTop")!;
  const heightDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight")!;
  let dropped = 0;
  Object.defineProperty(HTMLElement.prototype, "offsetTop", { configurable: true, get() {
    const index = Number((this.textContent ?? "").match(/line-(\d+)/)?.[1] ?? 0);
    return (index - dropped) * 20;
  } });
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 20 });
  try {
    node.scrollTop = 45;
    fireEvent.scroll(node);
    expect(screen.getByText("line-2").parentElement!.offsetTop - node.scrollTop).toBe(-5);
    dropped = 5;
    rerender(<LogViewer entries={[...entries.slice(5), ...Array.from({ length: 5 }, (_, i) => ({ message: `line-${2000 + i}`, timestamp: null, invocation_id: null }))]} text="" droppedCount={5} />);
    expect(screen.queryByText("line-2")).not.toBeInTheDocument();
    expect(screen.getByText("line-5").parentElement!.offsetTop - node.scrollTop).toBe(-5);
  } finally {
    Object.defineProperty(HTMLElement.prototype, "offsetTop", topDescriptor);
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", heightDescriptor);
  }
});
