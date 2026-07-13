// diff_view.js — pure helpers for the dual-solve file-changes view.
// no imports, so tests can execute the module directly under node
// (tests/test_web_diff_view.py); dual_solve.js consumes it in the browser.

// split a unified-diff string into per-file sections with +/-/context classes.
export function parseDiff(diff) {
  const files = [];
  let cur = null;
  for (const line of (diff || "").split("\n")) {
    if (line.startsWith("diff --git")) {
      const m = line.match(/ b\/(.+)$/);
      cur = { path: m ? m[1] : line, lines: [] };
      files.push(cur);
    } else if (!cur) {
      continue;
    } else if (
      // only the file-header markers, which always carry a trailing space and
      // path ("+++ b/x", "--- a/x"). a content line like "++counter" or
      // "---flag" must NOT be skipped.
      line.startsWith("+++ ") ||
      line.startsWith("--- ") ||
      line.startsWith("index ")
    ) {
      continue;
    } else if (line.startsWith("@@")) {
      cur.lines.push({ cls: "hunk", text: line });
    } else if (line.startsWith("+")) {
      cur.lines.push({ cls: "add", text: line });
    } else if (line.startsWith("-")) {
      cur.lines.push({ cls: "del", text: line });
    } else {
      cur.lines.push({ cls: "ctx", text: line });
    }
  }
  return files;
}

function sortNodes(nodes) {
  nodes.sort((a, b) => {
    if (a.type !== b.type) return a.type === "tree" ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  for (const n of nodes) if (n.children) sortNodes(n.children);
}

// build a nested file tree from a flat list of paths. nodes are folders-first
// then alphabetical at every level; intermediate directories are synthesized
// from path segments so a lone "a/b/c.py" still produces the a → b → c.py
// chain.
export function buildFileTree(paths) {
  const roots = [];
  // path -> node, so intermediate dirs are created once and reused.
  const byPath = new Map();

  for (const raw of paths) {
    const full = raw.replace(/^\/+|\/+$/g, "");
    if (!full) continue;
    const segs = full.split("/");
    let prefix = "";
    let siblings = roots;

    segs.forEach((seg, i) => {
      prefix = prefix ? `${prefix}/${seg}` : seg;
      const isLeaf = i === segs.length - 1;
      let node = byPath.get(prefix);
      if (!node) {
        node = isLeaf
          ? { name: seg, path: prefix, type: "blob" }
          : { name: seg, path: prefix, type: "tree", children: [] };
        byPath.set(prefix, node);
        siblings.push(node);
      }
      // descend; only directory nodes carry children.
      if (node.children) siblings = node.children;
    });
  }

  sortNodes(roots);
  return roots;
}

// flatten a tree into render-order rows for the rail: directories become
// non-interactive labels, files clickable rows; depth drives indentation.
export function flattenTree(nodes, depth = 0, out = []) {
  for (const n of nodes) {
    out.push({ name: n.name, path: n.path, type: n.type, depth });
    if (n.children) flattenTree(n.children, depth + 1, out);
  }
  return out;
}
