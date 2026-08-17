import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Renders assistant output as markdown.
 *
 * Models emit markdown whether or not you asked them to -- tables, headings,
 * fenced code, bold. Dropping that into a text node with `white-space: pre-wrap`
 * shows the *source*: a wall of pipes and dashes that is harder to read than the
 * plain prose it replaced. A question like "tell me about databases" comes back
 * as a dozen GFM tables and was, before this, unreadable.
 *
 * Two deliberate constraints:
 *
 * 1. **No raw HTML.** react-markdown ignores embedded HTML unless you add
 *    `rehype-raw`, and that is left out on purpose. Model output is untrusted
 *    input on its way into the DOM -- a prompt-injected `<img onerror=...>`
 *    should render as text, not execute. This is the whole XSS surface of the
 *    chat view, and the safe default is the one that needs no sanitiser.
 * 2. **GFM enabled.** Tables are the single most common structure in a technical
 *    answer, and plain CommonMark does not have them.
 *
 * Memoised on `content` because this re-renders on every streamed token: the
 * parse is cheap but not free, and the props are otherwise stable.
 */
export const Markdown = memo(function Markdown({ content }: { content: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Tables get their own scroll container. A six-column comparison
          // table cannot shrink to a narrow viewport, and letting it widen the
          // transcript instead makes the *whole page* scroll sideways.
          table: ({ children }) => (
            <div className="markdown__table-wrap">
              <table>{children}</table>
            </div>
          ),
          // Model-authored links point off-site; open them in a new tab and cut
          // the opener reference so the target cannot reach back into this page.
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
