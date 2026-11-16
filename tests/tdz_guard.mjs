// A let/const/class read in the same synchronous run before its declaration throws "Cannot access 'x' before
// initialization" (live 9d55e1057: weekGrid drew its month bands, whose pills read `pillParts`, before `const
// pillParts`). tsc reports a direct read (TS2448) but not one made through a local function. For each function body
// (and each module) this follows what runs synchronously from each statement: calls of the body's local functions,
// IIFEs, callbacks of array iterators, and inline callbacks given to a local function that calls that parameter at once
// (planner_week drawnMarks). It reports a later let/const/class of that body read on the way. Callbacks given to
// anything else (events, timers, menus, imported helpers) count as later, and names declared inside the followed code
// shadow, so it can miss a case but does not report working code.
// Usage: node tests/tdz_guard.mjs <file.ts>... ; prints JSON [{file, line, name, read}] (read: the call chain).
import fs from 'node:fs';
import { createRequire } from 'node:module';

const ts = createRequire(import.meta.url)('../node_modules/typescript');
const SYNC = new Set(['map', 'forEach', 'flatMap', 'filter', 'some', 'every', 'find', 'findIndex', 'findLast', 'reduce', 'sort', 'from']);
const fn = n => ts.isFunctionLike(n) && !!n.body;

function names(binding, out) {
  if (ts.isIdentifier(binding)) out.push(binding.text);
  else for (const e of binding.elements || []) if (!ts.isOmittedExpression(e)) names(e.name, out);
  return out;
}

// Every name declared anywhere inside `node` (parameters, variables, functions, classes, catch clauses).
function declared(node, out = new Set()) {
  const visit = n => {
    if ((ts.isVariableDeclaration(n) || ts.isParameter(n) || ts.isBindingElement(n)) && n.name) names(n.name, []).forEach(x => out.add(x));
    else if ((ts.isFunctionDeclaration(n) || ts.isClassDeclaration(n) || ts.isFunctionExpression(n)) && n.name) out.add(n.name.text);
    ts.forEachChild(n, visit);
  };
  visit(node);
  return out;
}

// An identifier that reads a variable (not a property, a declared name, an import/export alias or a type).
const isReference = n => {
  const p = n.parent;
  if (p.name === n && !ts.isShorthandPropertyAssignment(p)) return false;
  if ((ts.isBindingElement(p) || ts.isImportSpecifier(p) || ts.isExportSpecifier(p)) && p.propertyName === n) return false;
  return !(ts.isImportClause(p) || ts.isNamespaceImport(p) || ts.isExportSpecifier(p) || ts.isLabeledStatement(p)
    || ts.isBreakOrContinueStatement(p) || ts.isQualifiedName(p));
};

function check(file, text) {
  const source = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const found = [];
  const scope = body => {
    const statements = body.statements;
    const local = new Map(), late = new Map();
    statements.forEach((s, i) => {
      if (ts.isFunctionDeclaration(s) && s.name && s.body) local.set(s.name.text, s);
      if (ts.isClassDeclaration(s) && s.name) late.set(s.name.text, i);
      if (ts.isVariableStatement(s) && s.declarationList.flags & (ts.NodeFlags.Let | ts.NodeFlags.Const)) {
        for (const d of s.declarationList.declarations) {
          names(d.name, []).forEach(x => late.set(x, i));
          if (ts.isIdentifier(d.name) && d.initializer && (ts.isArrowFunction(d.initializer) || ts.isFunctionExpression(d.initializer))) local.set(d.name.text, d.initializer);
        }
      }
    });
    if (!late.size) return;
    // Which parameters a local function calls during its own run (memoised; a cycle answers "none" while open).
    const calledParams = new Map();
    const callsParam = (name, index) => {
      if (!calledParams.has(name)) {
        calledParams.set(name, new Set());
        const f = local.get(name), params = f.parameters.map(p => ts.isIdentifier(p.name) ? p.name.text : '');
        const visit = n => {
          if (n !== f && fn(n) && !runsNow(n)) return;
          if (ts.isCallExpression(n) && ts.isIdentifier(n.expression) && params.includes(n.expression.text)) calledParams.get(name).add(params.indexOf(n.expression.text));
          ts.forEachChild(n, visit);
        };
        ts.forEachChild(f, visit);
      }
      return calledParams.get(name).has(index);
    };
    // Does this function (or function name) run during the expression it appears in?
    const runsNow = (n, f = n) => {
      const p = n.parent;
      if (ts.isParenthesizedExpression(p)) return runsNow(p, f);
      if (ts.isCallExpression(p) && p.expression === n) return fn(f);  // (() => ...)()
      if (!ts.isCallExpression(p) || !p.arguments.includes(n)) return false;
      if (ts.isPropertyAccessExpression(p.expression)) return SYNC.has(p.expression.name.text);
      return fn(f) && ts.isIdentifier(p.expression) && local.has(p.expression.text) && callsParam(p.expression.text, p.arguments.indexOf(n));
    };
    statements.forEach((statement, j) => {
      if (ts.isFunctionDeclaration(statement)) return;  // declaring a function does not run it
      const seen = new Set();
      const walk = (node, shadow, via) => {
        const visit = n => {
          if (n !== node && fn(n) && !runsNow(n)) return;  // runs later (an event, a timer, a stored callback)
          if (ts.isTypeNode(n) || ts.isImportDeclaration(n)) return;
          if (ts.isIdentifier(n) && isReference(n) && !shadow.has(n.text)) {
            const at = late.get(n.text);
            if (at > j) found.push({ file, line: source.getLineAndCharacterOfPosition(n.getStart()).line + 1, name: n.text, read: via.join(' → ') || 'directly' });
            const target = local.get(n.text);
            const called = (ts.isCallExpression(n.parent) && n.parent.expression === n) || runsNow(n);
            if (target && called && !seen.has(n.text)) {
              seen.add(n.text);
              walk(target, new Set([...shadow, ...declared(target)]), [...via, n.text]);
            }
          }
          ts.forEachChild(n, visit);
        };
        if (fn(node)) ts.forEachChild(node, visit); else visit(node);
      };
      // Names the statement declares itself sit at its own index, so they never count as later.
      walk(statement, declared(statement), []);
    });
  };
  const visit = n => {
    if (ts.isSourceFile(n)) scope(n);
    else if (fn(n) && ts.isBlock(n.body)) scope(n.body);
    ts.forEachChild(n, visit);
  };
  visit(source);
  return found;
}

const report = process.argv.slice(2).flatMap(f => check(f, fs.readFileSync(f, 'utf8')));
console.log(JSON.stringify([...new Map(report.map(r => [`${r.file}:${r.line}:${r.name}`, r])).values()]));
