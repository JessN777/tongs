import type { ReactNode } from "react";
import { createLowlight } from "lowlight";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import shell from "highlight.js/lib/languages/shell";
import sql from "highlight.js/lib/languages/sql";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";
import type {
  DiffFileRow,
  DiffRow,
  SplitDiffCell,
} from "../../../shared/bridge.js";

/**
 * A file with more diff lines than this is drawn as plain text, so a large
 * file stays as fast as it was before highlighting existed.
 */
export const MAX_HIGHLIGHT_LINES = 2000;
/** The same cutoff for the file's diff text, counted in UTF-16 code units. */
export const MAX_HIGHLIGHT_CHARACTERS = 256 * 1024;

// Only this common set is bundled. A file in any other language stays plain.
const GRAMMARS = {
  bash,
  c,
  cpp,
  css,
  diff,
  dockerfile,
  go,
  ini,
  java,
  javascript,
  json,
  markdown,
  python,
  ruby,
  rust,
  shell,
  sql,
  typescript,
  xml,
  yaml,
} as const;

type GrammarName = keyof typeof GRAMMARS;

// The sidecar sends the Pygments alias of the file's lexer (for example
// "tsql" for a .sql file and "docker" for a Dockerfile). Each alias maps to
// the highlight.js grammar that reads the same language.
const PYGMENTS_GRAMMARS: ReadonlyMap<string, GrammarName> = new Map([
  ["python", "python"],
  ["python3", "python"],
  ["py", "python"],
  ["py3", "python"],
  ["typescript", "typescript"],
  ["ts", "typescript"],
  ["tsx", "typescript"],
  ["javascript", "javascript"],
  ["js", "javascript"],
  ["jsx", "javascript"],
  ["react", "javascript"],
  ["json", "json"],
  ["json-object", "json"],
  ["yaml", "yaml"],
  ["toml", "ini"],
  ["ini", "ini"],
  ["cfg", "ini"],
  ["dosini", "ini"],
  ["bash", "bash"],
  ["sh", "bash"],
  ["ksh", "bash"],
  ["zsh", "bash"],
  ["shell", "bash"],
  ["console", "shell"],
  ["shell-session", "shell"],
  ["go", "go"],
  ["golang", "go"],
  ["rust", "rust"],
  ["rs", "rust"],
  ["java", "java"],
  ["c", "c"],
  ["cpp", "cpp"],
  ["c++", "cpp"],
  ["css", "css"],
  ["html", "xml"],
  ["xml", "xml"],
  ["markdown", "markdown"],
  ["md", "markdown"],
  ["sql", "sql"],
  ["tsql", "sql"],
  ["t-sql", "sql"],
  ["mysql", "sql"],
  ["postgresql", "sql"],
  ["postgres", "sql"],
  ["docker", "dockerfile"],
  ["dockerfile", "dockerfile"],
  ["ruby", "ruby"],
  ["rb", "ruby"],
  ["diff", "diff"],
  ["udiff", "diff"],
]);

/** The highlight.js grammar for a Pygments alias, or null to stay plain. */
export function grammarFor(language: string | null): GrammarName | null {
  if (language === null) return null;
  return PYGMENTS_GRAMMARS.get(language.toLowerCase()) ?? null;
}

let lowlight: ReturnType<typeof createLowlight> | null = null;

function highlighter(): ReturnType<typeof createLowlight> {
  lowlight ??= createLowlight(GRAMMARS);
  return lowlight;
}

/**
 * A run of text in one line. `scopes` lists the highlight.js class names of
 * the spans around it, outermost first; an empty list is plain text.
 */
export interface SyntaxToken {
  readonly text: string;
  readonly scopes: readonly string[];
}

export type SyntaxLine = readonly SyntaxToken[];

type HastRoot = ReturnType<ReturnType<typeof createLowlight>["highlight"]>;
type HastNode = HastRoot["children"][number];

const NO_SCOPES: readonly string[] = Object.freeze([]);

/**
 * Highlights consecutive source lines as one text, so a construct that spans
 * lines (a block comment, a multi-line string) colours each of them, and
 * splits the result back into one token list per line. Returns null when the
 * grammar is unknown or the tokens do not reproduce the lines exactly.
 */
export function highlightLines(
  language: string | null,
  lines: readonly string[],
): readonly SyntaxLine[] | null {
  const grammar = grammarFor(language);
  if (grammar === null || lines.length === 0) return null;
  let root: HastRoot;
  try {
    root = highlighter().highlight(grammar, lines.join("\n"));
  } catch {
    return null;
  }
  const result: SyntaxToken[][] = [[]];
  const visit = (
    nodes: readonly HastNode[],
    scopes: readonly string[],
  ): void => {
    for (const node of nodes) {
      if (node.type === "text") {
        const parts = node.value.split("\n");
        parts.forEach((part, index) => {
          if (index > 0) result.push([]);
          if (part) result[result.length - 1]!.push({ text: part, scopes });
        });
      } else if (node.type === "element") {
        const name = classNameOf(node.properties["className"]);
        visit(node.children, name ? [...scopes, name] : scopes);
      }
    }
  };
  visit(root.children, NO_SCOPES);
  if (result.length !== lines.length) return null;
  for (let index = 0; index < lines.length; index += 1) {
    const text = result[index]!.map((token) => token.text).join("");
    if (text !== lines[index]) return null;
  }
  return result;
}

function classNameOf(value: unknown): string {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return "";
  return value.filter((item) => typeof item === "string").join(" ");
}

/**
 * The highlighted line of each source line row (unified) or cell (split) of
 * one file. A row or cell missing from it is drawn as plain text.
 */
export type FileSyntax = WeakMap<DiffRow | SplitDiffCell, SyntaxLine>;

/** One side of one hunk: its lines in order, and where each one is drawn. */
interface SideStream {
  readonly lines: string[];
  readonly targets: (DiffRow | SplitDiffCell | null)[];
}

interface CachedSyntax {
  readonly rowCount: number;
  readonly lastRow: DiffRow | undefined;
  readonly syntax: FileSyntax | null;
}

// Keyed by the file row, which a later page of the same diff keeps. The row
// count and last row tell a file whose rows were still arriving apart from
// the same file complete, so the file is highlighted again only when its rows
// change, never on a scroll, a selection or a switch back to it.
const cache = new WeakMap<DiffFileRow, CachedSyntax>();

/**
 * Highlights one file's rows, or returns null when the file has no supported
 * language or is past the size cutoff. The old side (context and removed
 * lines) and the new side (context and added lines) of each hunk are
 * highlighted as separate texts, the way the terminal diff reads them, so a
 * removed line that opens a string cannot colour the lines after it on the
 * new side. A unified context line takes its new-side colours.
 */
export function highlightFile(
  file: DiffFileRow | null,
  rows: readonly DiffRow[],
): FileSyntax | null {
  if (file === null) return null;
  const lastRow = rows[rows.length - 1];
  const cached = cache.get(file);
  if (
    cached !== undefined &&
    cached.rowCount === rows.length &&
    cached.lastRow === lastRow
  )
    return cached.syntax;
  const syntax = computeFileSyntax(file, rows);
  cache.set(file, { rowCount: rows.length, lastRow, syntax });
  return syntax;
}

function computeFileSyntax(
  file: DiffFileRow,
  rows: readonly DiffRow[],
): FileSyntax | null {
  if (grammarFor(file.language) === null || !withinLimits(rows)) return null;
  const hunks = new Map<number, { old: SideStream; new: SideStream }>();
  const streamsOf = (hunk: number): { old: SideStream; new: SideStream } => {
    let streams = hunks.get(hunk);
    if (!streams) {
      streams = {
        old: { lines: [], targets: [] },
        new: { lines: [], targets: [] },
      };
      hunks.set(hunk, streams);
    }
    return streams;
  };
  const push = (
    stream: SideStream,
    content: string,
    target: DiffRow | SplitDiffCell | null,
  ): void => {
    stream.lines.push(content);
    stream.targets.push(target);
  };
  for (const row of rows) {
    if (row.kind === "line") {
      const streams = streamsOf(row.hunk_index);
      if (row.line_type === "deletion") push(streams.old, row.content, row);
      else if (row.line_type === "addition")
        push(streams.new, row.content, row);
      else if (row.line_type === "context") {
        push(streams.old, row.content, null);
        push(streams.new, row.content, row);
      }
    } else if (row.kind === "split") {
      const streams = streamsOf(row.hunk_index);
      if (row.old && row.old.line_type !== "no_newline")
        push(streams.old, row.old.content, row.old);
      if (row.new && row.new.line_type !== "no_newline")
        push(streams.new, row.new.content, row.new);
    }
  }
  const syntax: FileSyntax = new WeakMap();
  for (const streams of hunks.values()) {
    for (const stream of [streams.old, streams.new]) {
      if (!stream.targets.some((target) => target !== null)) continue;
      const lines = highlightLines(file.language, stream.lines);
      if (lines === null) continue;
      stream.targets.forEach((target, index) => {
        if (target !== null) syntax.set(target, lines[index]!);
      });
    }
  }
  return syntax;
}

/**
 * True when the file's diff lines are within both cutoffs. A context line is
 * counted once in either layout, so the cutoff does not depend on the layout.
 */
function withinLimits(rows: readonly DiffRow[]): boolean {
  let lines = 0;
  let characters = 0;
  const count = (content: string): boolean => {
    lines += 1;
    characters += content.length;
    return (
      lines <= MAX_HIGHLIGHT_LINES && characters <= MAX_HIGHLIGHT_CHARACTERS
    );
  };
  for (const row of rows) {
    if (row.kind === "line") {
      if (row.line_type !== "no_newline" && !count(row.content)) return false;
    } else if (row.kind === "split") {
      const old = row.old?.line_type === "no_newline" ? null : row.old;
      const fresh = row.new?.line_type === "no_newline" ? null : row.new;
      const shared =
        old !== null &&
        fresh !== null &&
        old.line_type === "context" &&
        fresh.line_type === "context";
      if (old !== null && !count(old.content)) return false;
      if (fresh !== null && !shared && !count(fresh.content)) return false;
    }
  }
  return true;
}

/**
 * A line's content, as highlighted spans when `line` is given and as plain
 * text otherwise. Classes only, never markup from a string, so the renderer
 * content security policy is unchanged.
 */
export function SyntaxContent({
  line,
  text,
}: {
  readonly line: SyntaxLine | undefined;
  readonly text: string;
}): ReactNode {
  if (line === undefined) return text;
  return line.map((token, index) =>
    token.scopes.length === 0 ? (
      token.text
    ) : (
      <ScopedToken key={index} token={token} />
    ),
  );
}

function ScopedToken({ token }: { readonly token: SyntaxToken }): ReactNode {
  return token.scopes.reduceRight<ReactNode>(
    (inner, scope) => <span className={scope}>{inner}</span>,
    token.text,
  );
}
