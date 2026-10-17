// Verifies that the workspace tooling configuration matches the packages that
// actually exist, so that adding, renaming or removing a package cannot leave
// stale or missing entries behind. Run with `yarn check:workspace`.
//
// Workspaces live in two directories:
//   - packages/<dir>: our own packages, named `@llmchip/<dir>`. They export "."
//     with the conditions development -> ./src/index.ts, types ->
//     ./lib/index.d.ts, default -> ./lib/index.js, in that order.
//   - vendor/<dir>: vendored third-party source (see vendor/README.md). They
//     keep their upstream npm names and are consumed from their build output,
//     so their "." export must not have a `development` condition. Each one
//     must ship its upstream LICENSE and be listed in the vendor/README.md
//     manifest table.
//
// Every workspace must have package.json (private, type module), tsconfig.json
// and src/index.ts, and be referenced from tsconfig.build.json. Every workspace
// package imported from src/ must be declared as a `workspace:*` dependency
// (dependencies, peerDependencies or devDependencies) and have a matching
// reference in the importer's tsconfig.json. Conversely, tsconfig.build.json
// must not reference anything that is not a workspace, and packages/ must not
// declare workspace dependencies they never import.

import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const packageExports = {
  development: './src/index.ts',
  types: './lib/index.d.ts',
  default: './lib/index.js',
}
const problems: string[] = []

interface Workspace {
  /** Path relative to the repository root, e.g. `packages/kernel`. */
  path: string
  kind: 'packages' | 'vendor'
  pkg: any
}

function readJson(path: string): any {
  return JSON.parse(readFileSync(path, 'utf8'))
}

function subdirs(dir: string): string[] {
  const abs = join(root, dir)
  if (!existsSync(abs)) return []
  return readdirSync(abs)
    .filter((name) => statSync(join(abs, name)).isDirectory())
    .sort()
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

/** Bare package name of a module specifier: `@scope/name/sub` -> `@scope/name`. */
function packageOf(specifier: string): string {
  const parts = specifier.split('/')
  return specifier.startsWith('@') ? parts.slice(0, 2).join('/') : parts[0]!
}

const workspaces: Workspace[] = []
for (const kind of ['packages', 'vendor'] as const) {
  for (const dir of subdirs(kind)) {
    const path = `${kind}/${dir}`
    const missing = ['package.json', 'tsconfig.json', 'src/index.ts'].filter((file) => !existsSync(join(root, path, file)))
    if (kind === 'vendor' && !existsSync(join(root, path, 'LICENSE'))) missing.push('LICENSE')
    for (const file of missing) problems.push(`${path}: missing ${file}`)
    if (missing.includes('package.json') || missing.includes('tsconfig.json')) continue
    workspaces.push({ path, kind, pkg: readJson(join(root, path, 'package.json')) })
  }
}
const byName = new Map(workspaces.map((ws) => [ws.pkg.name as string, ws]))
const buildRefs = refPaths(join(root, 'tsconfig.build.json'))
const vendorReadme = existsSync(join(root, 'vendor/README.md')) ? readFileSync(join(root, 'vendor/README.md'), 'utf8') : ''

for (const ws of workspaces) {
  const { path, kind, pkg } = ws
  const dir = path.slice(kind.length + 1)

  if (kind === 'packages' && pkg.name !== `@llmchip/${dir}`) {
    problems.push(`${path}: package name is ${pkg.name}, expected @llmchip/${dir}`)
  }
  if (pkg.private !== true) problems.push(`${path}: package.json must set "private": true`)
  if (pkg.type !== 'module') problems.push(`${path}: package.json must set "type": "module"`)

  const dotExport = pkg.exports?.['.']
  if (kind === 'packages') {
    if (JSON.stringify(dotExport) !== JSON.stringify(packageExports)) {
      problems.push(`${path}: exports["."] must be ${JSON.stringify(packageExports)}`)
    }
  } else {
    if (typeof dotExport?.types !== 'string' || !dotExport.types.startsWith('./lib/')) {
      problems.push(`${path}: exports["."].types must point into ./lib/`)
    }
    if (dotExport && 'development' in dotExport) {
      problems.push(`${path}: vendored packages are consumed from lib/ and must not export a "development" condition`)
    }
    if (!vendorReadme.includes(`| \`${dir}/\` | \`${pkg.name}\` |`)) {
      problems.push(`vendor/README.md: manifest table has no row for \`${dir}/\` (\`${pkg.name}\`)`)
    }
  }

  if (!buildRefs.has(`./${path}`)) problems.push(`tsconfig.build.json: missing reference to ./${path}`)

  const declared: Record<string, string> = { ...pkg.devDependencies, ...pkg.peerDependencies, ...pkg.dependencies }
  const tsRefs = refPaths(join(root, path, 'tsconfig.json'))
  const imported = new Set<string>()
  for (const file of sourceFiles(join(root, path, 'src'))) {
    for (const match of readFileSync(file, 'utf8').matchAll(/\b(?:from|import)\s*\(?\s*['"]([^'"./][^'"]*)['"]/g)) {
      const name = packageOf(match[1]!)
      if (name !== pkg.name && byName.has(name)) imported.add(name)
    }
  }
  for (const dep of imported) {
    const target = byName.get(dep)!
    const refPath = relative(join(root, path), join(root, target.path))
    if (declared[dep] !== 'workspace:*') problems.push(`${path}: imports ${dep} but does not declare it as "workspace:*"`)
    if (!tsRefs.has(refPath)) problems.push(`${path}/tsconfig.json: imports ${dep} but has no reference to ${refPath}`)
  }
  for (const [dep, range] of Object.entries(declared)) {
    if (byName.has(dep) && range !== 'workspace:*') {
      problems.push(`${path}: workspace package ${dep} must be declared as "workspace:*", not "${range}"`)
    }
    // Vendored manifests keep upstream declarations even where they are broader than the imports.
    if (kind === 'packages' && byName.has(dep) && !imported.has(dep)) {
      problems.push(`${path}: declares ${dep} but never imports it`)
    }
  }
}

const workspacePaths = new Set(workspaces.map((ws) => `./${ws.path}`))
for (const ref of buildRefs) {
  if (!workspacePaths.has(ref)) problems.push(`tsconfig.build.json: reference ${ref} is not a workspace under packages/ or vendor/`)
}

if (problems.length > 0) {
  console.error('Workspace check failed:')
  for (const problem of problems) console.error(`  - ${problem}`)
  process.exitCode = 1
} else {
  const count = (kind: Workspace['kind']) => workspaces.filter((ws) => ws.kind === kind).length
  console.log(`Workspace check passed: ${count('packages')} packages, ${count('vendor')} vendored packages.`)
}
