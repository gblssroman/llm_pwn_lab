(function registerMarkdownRenderer(global) {
  "use strict";

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function safeHref(value) {
    const href = String(value).trim();
    if (/^(https?:\/\/|mailto:)/i.test(href)) return escapeHtml(href);
    return null;
  }

  function renderInline(value) {
    const stored = [];
    const hold = (html) => `\u0000${stored.push(html) - 1}\u0000`;
    let source = String(value);

    source = source.replace(/`([^`\n]+)`/g, (_, code) => hold(`<code>${escapeHtml(code)}</code>`));
    source = source.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_, label, rawHref) => {
      const href = safeHref(rawHref);
      if (!href) return `${label} (${rawHref})`;
      return hold(`<a href="${href}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
    });

    source = escapeHtml(source)
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s.,!?;:)])/g, "$1<em>$2</em>");

    return source.replace(/\u0000(\d+)\u0000/g, (_, index) => stored[Number(index)]);
  }

  function renderCodeBlock(codeLines, language) {
    const safeLanguage = /^[A-Za-z0-9_+.-]+$/.test(language || "") ? language : "";
    const className = safeLanguage ? ` class="language-${escapeHtml(safeLanguage)}"` : "";
    const label = safeLanguage ? escapeHtml(safeLanguage) : "code";
    return `<div class="code-block"><div class="code-block-label">${label}</div><pre><code${className}>${escapeHtml(codeLines.join("\n"))}</code></pre></div>`;
  }

  function splitTableRow(value) {
    const source = String(value).trim();
    const cells = [];
    let cell = "";
    let codeTicks = 0;
    let separators = 0;

    for (let index = 0; index < source.length; index += 1) {
      const character = source[index];
      if (character === "\\" && source[index + 1] === "|") {
        cell += "|";
        index += 1;
        continue;
      }
      if (character === "`") {
        let runLength = 1;
        while (source[index + runLength] === "`") runLength += 1;
        if (codeTicks === 0) codeTicks = runLength;
        else if (codeTicks === runLength) codeTicks = 0;
        cell += "`".repeat(runLength);
        index += runLength - 1;
        continue;
      }
      if (character === "|" && codeTicks === 0) {
        cells.push(cell.trim());
        cell = "";
        separators += 1;
        continue;
      }
      cell += character;
    }
    cells.push(cell.trim());

    if (!separators) return null;
    if (source.startsWith("|") && cells[0] === "") cells.shift();
    if (cells.at(-1) === "") cells.pop();
    return cells;
  }

  function tableAlignment(marker) {
    const value = marker.trim();
    if (value.startsWith(":") && value.endsWith(":")) return "align-center";
    if (value.endsWith(":")) return "align-right";
    return "align-left";
  }

  function tableDelimiter(value, columnCount) {
    const cells = splitTableRow(value);
    if (!cells || cells.length !== columnCount) return null;
    if (!cells.every((cell) => /^:?-{3,}:?$/.test(cell))) return null;
    return cells.map(tableAlignment);
  }

  function renderTable(headers, alignments, rows) {
    const renderRow = (cells, tag) => `<tr>${headers.map((_, index) => {
      const content = cells[index] === undefined ? "" : cells[index];
      return `<${tag} class="${alignments[index]}">${renderInline(content)}</${tag}>`;
    }).join("")}</tr>`;
    const head = `<thead>${renderRow(headers, "th")}</thead>`;
    const body = rows.length ? `<tbody>${rows.map((row) => renderRow(row, "td")).join("")}</tbody>` : "";
    return `<div class="markdown-table-wrap"><table>${head}${body}</table></div>`;
  }

  function renderMarkdown(value) {
    const lines = String(value || "").replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n");
    const output = [];
    let paragraph = [];
    let listType = null;
    let listItems = [];

    const flushParagraph = () => {
      if (!paragraph.length) return;
      output.push(`<p>${paragraph.map(renderInline).join("<br>")}</p>`);
      paragraph = [];
    };
    const flushList = () => {
      if (!listType) return;
      output.push(`<${listType}>${listItems.map((item) => `<li>${renderInline(item)}</li>`).join("")}</${listType}>`);
      listType = null;
      listItems = [];
    };
    const flushText = () => {
      flushParagraph();
      flushList();
    };

    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      const fence = line.match(/^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+.-]*)\s*$/);
      if (fence) {
        flushText();
        const marker = fence[1][0];
        const minimumLength = fence[1].length;
        const codeLines = [];
        index += 1;
        while (index < lines.length) {
          const closing = lines[index].match(/^\s*(`{3,}|~{3,})\s*$/);
          if (closing && closing[1][0] === marker && closing[1].length >= minimumLength) break;
          codeLines.push(lines[index]);
          index += 1;
        }
        output.push(renderCodeBlock(codeLines, fence[2]));
        continue;
      }

      if (!line.trim()) {
        flushText();
        continue;
      }

      const tableHeaders = splitTableRow(line);
      const tableAlignments = tableHeaders && index + 1 < lines.length
        ? tableDelimiter(lines[index + 1], tableHeaders.length)
        : null;
      if (tableHeaders && tableAlignments) {
        flushText();
        const rows = [];
        let rowIndex = index + 2;
        while (rowIndex < lines.length) {
          const cells = splitTableRow(lines[rowIndex]);
          if (!cells) break;
          rows.push(cells);
          rowIndex += 1;
        }
        output.push(renderTable(tableHeaders, tableAlignments, rows));
        index = rowIndex - 1;
        continue;
      }

      const heading = line.match(/^\s*(#{1,6})\s+(.+)$/);
      if (heading) {
        flushText();
        const level = heading[1].length;
        output.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        continue;
      }

      if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
        flushText();
        output.push("<hr>");
        continue;
      }

      const unordered = line.match(/^\s*[-+*]\s+(.+)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (unordered || ordered) {
        flushParagraph();
        const nextType = unordered ? "ul" : "ol";
        if (listType && listType !== nextType) flushList();
        listType = nextType;
        listItems.push((unordered || ordered)[1]);
        continue;
      }

      const quote = line.match(/^\s*>\s?(.*)$/);
      if (quote) {
        flushText();
        output.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
        continue;
      }

      flushList();
      paragraph.push(line);
    }

    flushText();
    return output.join("\n");
  }

  global.AgentRelayMarkdown = Object.freeze({ render: renderMarkdown });
}(globalThis));
