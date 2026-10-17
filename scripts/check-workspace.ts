// Verifies that the workspace tooling configuration matches the packages that
// actually exist, so that adding, renaming or removing a package cannot leave
// stale or missing entries behind. Run with `yarn check:workspace`.
//
// Every directory under packages/ must:
//   - have package.json (name `@llmchip/<dir>`, private, type module),
//     tsconfig.json and src/index.ts;
//   - export "." with the conditions development -> ./src/index.ts,
//     types -> ./lib/index.d.ts, default -> ./lib/index.js, in that order;
//   - be referenced from tsconfig.build.json;
//   - declare every `@llmchip/*` package it imports from src/ as a
//     `workspace:*` dependency, and reference that package's tsconfig.
// Conversely, tsconfig.build.json must not reference anything that is not a
// package, and packages must not declare workspace dependencies they never import.

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const scope = '@llmchip/'
const expectedExports = {
  development: './src/index.ts',
  types: './lib/index.d.ts',
  default: './lib/index.js',
}
const problems: string[] = []

function readJson(path: string): any {
  return JSON.parse(readFileSync(path, 'utf8'))
}

function sourceFiles(dir: string): string[] {
  if (!existsSync(dir)) return []
  return readdirSync(dir, { recursive: true, encoding: 'utf8' })
    .filter((name) => name.endsWith('.ts'))
    .map((name) => join(dir, name))
}

function refPaths(tsconfigPath: string): Set<string> {
  const refs: { path: string }[] = readJson(tsconfigPath).references ?? []
  return new Set(refs.map((ref) => ref.path))
}

const packageDirs = readdirSync(join(root, 'packages'))
  .filter((name) => statSync(join(root, 'packages', name)).isDirectory())
  .sort()
const packageNames = new Set(packageDirs.map((dir) => scope + dir))
const buildRefs = refPaths(join(root, 'tsconfig.build.json'))

for (const dir of packageDirs) {
  const pkgDir = join(root, 'packages', dir)
  const name = scope + dir
  const where = `packages/${dir}`

  const missing = ['package.json', 'tsconfig.json', 'src/index.ts'].filter((file) => !existsSync(join(pkgDir, file)))
  for (const file of missing) problems.push(`${where}: missing ${file}`)
  if (missing.includes('package.json') || missing.includes('tsconfig.json')) continue

  const pkg = readJson(join(pkgDir, 'package.json'))
  if (pkg.name !== name) problems.push(`${where}: package name is ${pkg.name}, expected ${name}`)
  if (pkg.private !== true) problems.push(`${where}: package.json must set "private": true`)
  if (pkg.type !== 'module') problems.push(`${where}: package.json must set "type": "module"`)
  if (JSON.stringify(pkg.exports?.['.']) !== JSON.stringify(expectedExports)) {
    problems.push(`${where}: exports["."] must be ${JSON.stringify(expectedExports)}`)
  }

  if (!buildRefs.has(`./packages/${dir}`)) {
    problems.push(`tsconfig.build.json: missing reference to ./packages/${dir}`)
  }

  const deps: Record<string, string> = pkg.dependencies ?? {}
  const tsRefs = refPaths(join(pkgDir, 'tsconfig.json'))
  const imported = new Set<string>()
  for (const file of sourceFiles(join(pkgDir, 'src'))) {
    for (const match of readFileSync(file, 'utf8').matchAll(/\bfrom\s+['"](@llmchip\/[^'"/]+)/g)) {
      if (match[1] !== name) imported.add(match[1]!)
    }
  }
  for (const dep of imported) {
    const depDir = dep.slice(scope.length)
    if (!packageNames.has(dep)) {
      problems.push(`${where}: imports ${dep}, which is not a workspace package`)
      continue
    }
    if (deps[dep] !== 'workspace:*') problems.push(`${where}: imports ${dep} but does not declare it as "workspace:*"`)
    if (!tsRefs.has(`../${depDir}`)) problems.push(`${where}/tsconfig.json: imports ${dep} but has no reference to ../${depDir}`)
  }
  for (const dep of Object.keys(deps)) {
    if (dep.startsWith(scope) && !imported.has(dep)) problems.push(`${where}: declares ${dep} but never imports it`)
  }
}

for (const ref of buildRefs) {
  const dir = ref.replace(/^\.\/packages\//, '')
  if (dir === ref || !packageDirs.includes(dir)) {
    problems.push(`tsconfig.build.json: reference ${ref} is not a package under packages/`)
  }
}

if (problems.length > 0) {
  console.error('Workspace check failed:')
  for (const problem of problems) console.error(`  - ${problem}`)
  process.exitCode = 1
} else {
  console.log(`Workspace check passed: ${packageDirs.length} packages (${packageDirs.join(', ')}).`)
}
