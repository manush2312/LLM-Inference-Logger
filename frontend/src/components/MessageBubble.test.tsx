/**
 * How assistant output is rendered.
 *
 * Models emit markdown unprompted, so the choice is not "markdown or plain
 * text" -- it is "rendered markdown or visible markdown source". These lock in
 * the three decisions that came out of that:
 *
 *   - structure renders as structure (a GFM table becomes a <table>)
 *   - raw HTML in model output never becomes live DOM
 *   - the user's own words are shown back verbatim
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MessageBubble } from "./MessageBubble";
import type { Message } from "../api/types";

function message(role: Message["role"], content: string): Message {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    role,
    content,
    seq: 0,
    created_at: new Date().toISOString(),
  } as Message;
}

describe("assistant output", () => {
  it("renders a GFM table as a table, not as pipes and dashes", () => {
    // Shaped like the real thing: the answer to "tell me about databases" comes
    // back as table after table, and this is exactly what used to arrive on
    // screen as raw source.
    const { container } = render(
      <MessageBubble
        message={message(
          "assistant",
          [
            "| Engine | Model |",
            "| --- | --- |",
            "| PostgreSQL | Relational |",
            "| Redis | Key-Value |",
          ].join("\n"),
        )}
      />,
    );

    expect(container.querySelector("table")).not.toBeNull();
    expect(screen.getByRole("columnheader", { name: "Engine" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "PostgreSQL" })).toBeInTheDocument();
    // The separator row is syntax, so it must not survive as content.
    expect(container.textContent).not.toContain("---");
  });

  it("puts tables in a scroll container so a wide one cannot widen the page", () => {
    // Regression guard for layout, not decoration: `overflow-x` on a <table>
    // does nothing, so without the wrapper a wide table scrolls the whole page
    // sideways instead of itself.
    const { container } = render(
      <MessageBubble
        message={message("assistant", "| a | b |\n| --- | --- |\n| 1 | 2 |")}
      />,
    );
    const wrap = container.querySelector(".markdown__table-wrap");
    expect(wrap).not.toBeNull();
    expect(wrap!.querySelector("table")).not.toBeNull();
  });

  it("renders fenced code as a code block", () => {
    const { container } = render(
      <MessageBubble
        message={message("assistant", "```sql\nSELECT 1;\n```")}
      />,
    );
    const pre = container.querySelector("pre code");
    expect(pre).not.toBeNull();
    expect(pre!.textContent).toContain("SELECT 1;");
  });

  it("does not turn raw HTML from the model into live DOM", () => {
    // The security assertion. Model output is untrusted input on its way into
    // the DOM -- a prompt-injected payload must stay inert. This passes because
    // `rehype-raw` is deliberately absent from Markdown.tsx; anyone adding it to
    // support inline HTML breaks this test, which is the point.
    const { container } = render(
      <MessageBubble
        message={message(
          "assistant",
          'Here: <img src=x onerror="window.__pwned=1"> and <script>window.__pwned=2</script>',
        )}
      />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("survives the half-finished markdown that streaming always produces", () => {
    // Mid-stream the text is arbitrarily truncated: an unclosed fence, half a
    // table row. The parser must cope rather than throw, because this state
    // exists on literally every token.
    for (const partial of ["```sql\nSELECT", "| Engine | Mod", "**bold stil"]) {
      expect(() =>
        render(<MessageBubble message={message("assistant", partial)} />),
      ).not.toThrow();
    }
  });
});

describe("the user's own message", () => {
  it("is shown verbatim, not parsed as markdown", () => {
    // Someone asking about `**` in a language should see `**`, not watch their
    // question turn bold.
    const { container } = render(
      <MessageBubble message={message("user", "what does **kwargs mean?")} />,
    );
    expect(container.querySelector("strong")).toBeNull();
    expect(screen.getByText("what does **kwargs mean?")).toBeInTheDocument();
  });
});
