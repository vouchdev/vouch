// diffParse.ts — split a unified-diff string into per-file sections.
// extracted from Diff.tsx so the diff renderer and the file-changes view share
// one parser. behavior is unchanged from the original Diff.tsx parseDiff.

export interface DiffLine {
  cls: 'hunk' | 'add' | 'del' | 'ctx'
  text: string
}

export interface DiffFile {
  head: string
  lines: DiffLine[]
}

export function parseDiff(diff: string): DiffFile[] {
  const files: DiffFile[] = []
  let cur: DiffFile | null = null

  for (const line of diff.split('\n')) {
    if (line.startsWith('diff --git')) {
      const m = line.match(/ b\/(.+)$/)
      cur = { head: m ? m[1] : line, lines: [] }
      files.push(cur)
    } else if (!cur) {
      continue
    } else if (
      line.startsWith('+++') ||
      line.startsWith('---') ||
      line.startsWith('index ')
    ) {
      continue
    } else {
      const cls: DiffLine['cls'] = line.startsWith('@@')
        ? 'hunk'
        : line.startsWith('+')
          ? 'add'
          : line.startsWith('-')
            ? 'del'
            : 'ctx'
      cur.lines.push({ cls, text: line || ' ' })
    }
  }

  return files
}
