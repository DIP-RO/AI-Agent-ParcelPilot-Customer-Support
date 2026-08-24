/** Minimal, dependency-free markdown renderer (escape-first, so it is XSS-safe). */

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function inline(s: string): string {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
}

export function renderMarkdown(text: string): string {
  const lines = escapeHtml(text).split("\n");
  const out: string[] = [];
  let listMode: "ul" | "ol" | null = null;
  let inCode = false;

  const closeList = () => {
    if (listMode) {
      out.push(listMode === "ul" ? "</ul>" : "</ol>");
      listMode = null;
    }
  };

  for (const raw of lines) {
    if (raw.trim().startsWith("```")) {
      closeList();
      out.push(inCode ? "</code></pre>" : "<pre><code>");
      inCode = !inCode;
      continue;
    }
    if (inCode) {
      out.push(raw + "\n");
      continue;
    }
    const h = raw.match(/^(#{1,4})\s+(.*)$/);
    const ul = raw.match(/^\s*[-•]\s+(.*)$/);
    const ol = raw.match(/^\s*\d+[.)]\s+(.*)$/);
    if (h) {
      closeList();
      const level = Math.min(h[1].length + 2, 5);
      out.push(`<h${level}>${inline(h[2])}</h${level}>`);
    } else if (ul) {
      if (listMode !== "ul") {
        closeList();
        out.push("<ul>");
        listMode = "ul";
      }
      out.push(`<li>${inline(ul[1])}</li>`);
    } else if (ol) {
      if (listMode !== "ol") {
        closeList();
        out.push("<ol>");
        listMode = "ol";
      }
      out.push(`<li>${inline(ol[1])}</li>`);
    } else if (raw.trim() === "") {
      closeList();
    } else {
      closeList();
      out.push(`<p>${inline(raw)}</p>`);
    }
  }
  closeList();
  if (inCode) out.push("</code></pre>");
  return out.join("");
}

export function Markdown({ text }: { text: string }) {
  return <div className="md" dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }} />;
}
